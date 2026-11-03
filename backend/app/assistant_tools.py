"""【学习标注：Agent 只读工具】助手和 MCP 共用的检索、版本、质量、任务和样例读取。

这里是 Agent 的“眼睛”。每个工具都从数据库读取真实结果，并在读取时检查当前权限；
数据集文本只作为不可信内容返回，不能成为系统指令。
"""
import re

from sqlalchemy import func, select

from .common import fetch, public_row, require
from .datasets import search_catalog
from .models import Asset, Dataset, Job, Version
from .policy import can_read_content, can_view_version
from .tasks import authorized_job


MODALITIES = {"image": ("图像", "图片", "image"), "audio": ("语音", "音频", "audio"),
              "video": ("视频", "video"), "tabular": ("表格", "csv", "tabular"),
              "text": ("文本", "语料", "text"), "multimodal": ("多模态", "multimodal")}


def search(db, user, settings, question, *, offset=0, limit=8):
    """Deterministic keyword ranking for offline use; never substitute unrelated results."""
    q = question.lower().strip()
    modality = next((k for k, words in MODALITIES.items() if any(w in q for w in words)), None)
    cleaned = q
    for word in ("帮我", "寻找", "查找", "搜索", "推荐", "适合", "有哪些", "有没有", "数据集", "数据", "当前", "可申请", "查看", "列出", "全部", "目录"):
        cleaned = cleaned.replace(word, " ")
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", cleaned))
    terms |= {word[i:i+2] for word in list(terms) if re.fullmatch(r"[\u4e00-\u9fff]+", word)
              for i in range(len(word)-1)}
    access_mode = 'application' if '可申请' in q else (
        'open' if any(word in q for word in ('开放下载', '开放数据')) else None)
    return search_catalog(db, user, settings, modality=modality, terms=terms, access_mode=access_mode, limit=limit, offset=offset)


def search_page(db, user, settings, query, offset=0, limit=8):
    rows = search(db, user, settings, query, offset=offset, limit=limit + 1)
    has_more = len(rows) > limit
    return {'items': rows[:limit], 'offset': offset, 'limit': limit, 'returned': min(len(rows), limit),
            'has_more': has_more, 'next_offset': offset + limit if has_more else None,
            'total_is_unknown': True, 'snapshot_isolation': False}


def visible_version(db, user, settings, version_id):
    version = fetch(db, Version, version_id)
    ds = fetch(db, Dataset, version.dataset_id)
    require(ds.status != "blocked" and can_view_version(db, user, version, settings), "版本不存在或不可见", 404)
    return version, ds


def version_info(db, user, settings, version_id, allow_content=True):
    v, ds = visible_version(db, user, settings, version_id)
    count, total = db.execute(select(func.count(Asset.id), func.coalesce(func.sum(Asset.size), 0))
                             .where(Asset.version_id == v.id)).one()
    return {"dataset_id": ds.id, "version_id": v.id, "name": ds.name, "number": v.number,
            "status": v.status, "description": ds.description[:600], "modality": ds.modality,
            "license": v.license, "access_mode": ds.access_mode, "source": v.source[:600],
            "asset_count": int(count), "total_bytes": int(total),
            "task": v.card.get("task", "未填写"), "split": v.card.get("split", "未填写"),
            "can_read": allow_content and can_read_content(db, user, v, settings),
            "url": f"/datasets/{ds.id}?version={v.id}"}


def quality_info(db, user, settings, version_id):
    version, _ = visible_version(db, user, settings, version_id)
    require(can_read_content(db, user, version, settings), "质量报告需要该版本授权并确认使用许可")
    from .datasets import latest_quality_report
    report = latest_quality_report(db,version.id)
    if not report:
        return {"version_id": version.id, "status": "not_checked", "explanation": "该版本尚未生成质量报告，可以发起质量检查。"}
    q = report['summary']
    total, checked = q.get("total", 0), q.get("checked", 0)
    return {"version_id": version.id, "report_id": report['id'], "created_at": report['created_at'].isoformat(),
            "status": "available", "method": q.get("method"), "model": q.get("model"),
            "total": total, "checked": checked, "passed": q.get("passed", 0),
            "failed": q.get("failed", 0), "duplicate_files": q.get("duplicate_files", 0),
            "not_checked": q.get("not_checked", []),
            "explanation": f"检查覆盖 {checked}/{total} 个文件，通过 {q.get('passed', 0)} 个，"
                           f"失败 {q.get('failed', 0)} 个，重复文件 {q.get('duplicate_files', 0)} 个。"
                           + ("采用模型检测。" if q.get("model") else "采用规则检查，未进行模型语义检测。")
                           + "未检测项目不能视为通过。"}


def job_info(db, user, settings, job):
    require(authorized_job(db, user, job, settings), "任务不存在或授权已失效", 404)
    v, ds = visible_version(db, user, settings, job.version_id)
    return {**public_row(job), "dataset_name": ds.name, "version_number": v.number}


def sample_info(db, user, settings, storage, asset_id):
    asset = fetch(db, Asset, asset_id)
    version, _ = visible_version(db, user, settings, asset.version_id)
    require(can_read_content(db, user, version, settings) or
            (version.status == "published" and asset.preview_allowed), "该样例未授权预览")
    require(asset.status == "clean", "文件检查未通过", 409)
    from .evidence_access import is_truth
    from .policy import manage_dataset
    require(not is_truth(db,asset),
            '评测真值不是 Agent 可读取的训练样例',403)
    ext = asset.filename.rsplit(".", 1)[-1].lower()
    require(ext in {"txt", "md", "csv", "json", "jsonl"}, "Agent 样例工具目前仅支持文本与表格，请在详情页预览其他格式", 400)
    raw = b"".join(storage.chunks(asset.object_key, 0, min(asset.size, 16384) - 1))
    return {"asset_id": asset.id, "version_id": version.id, "filename": asset.filename,
            "content": "\n".join(raw.decode("utf-8", errors="replace").splitlines()[:20]),
            "truncated": True, "content_is_untrusted": True}
