"""【学习标注：受控预览与下载】用资产 ID 访问文件，每次请求都重新校验版本授权。

download 支持 HTTP Range 断点续传；preview 只返回有限文本或压缩图像，不暴露存储路径。
"""
import base64
import io
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request

from .common import audit, fetch, require
from .db import get_db
from .models import Asset, Dataset, Version
from .policy import can_read_content, can_view_version, review_publication
from .security import current_user, enforce_scope, optional_user
from .concurrency import media_slot

router = APIRouter(tags=["受控下载与预览"])


def byte_range(value, size):
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    require(match and any(match.groups()), "不支持此 Range", 416)
    left, right = match.groups()
    if not left:
        length = int(right)
        require(length > 0, "无效 Range", 416)
        start, end = max(0, size - length), size - 1
    else:
        start, end = int(left), min(int(right), size - 1) if right else size - 1
    require(0 <= start <= end < size, "Range 超出文件范围", 416)
    return start, end, True


def prepare_download(db, asset_id, request, record=True):
    user = current_user(optional_user(request, db))
    enforce_scope(request, "content:read")
    asset = fetch(db, Asset, asset_id)
    version = fetch(db, Version, asset.version_id)
    from .evidence_access import can_read_asset
    require(can_read_asset(db, user, asset, request.app.state.settings), "尚未获得此文件授权；评测真值不随训练版本授权开放")
    require(asset.status == "clean", "文件检查未通过", 409)
    start, end, partial = byte_range(request.headers.get("range"), asset.size)
    if record:
        audit(db, user, "asset.download", asset.id, db.get(Dataset, version.dataset_id).space_id,
              version_id=version.id, start=start, end=end)
        db.commit()
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(asset.filename, safe='')}",
               "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
               "ETag": f'"{asset.sha256}"', "Cache-Control": "private, no-store",
               "X-Content-Type-Options": "nosniff"}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{asset.size}"
    return {'key': asset.object_key, 'start': start, 'end': end, 'headers': headers,
            'status_code': 206 if partial else 200}


@router.get("/assets/{asset_id}/download")
async def download(asset_id: str, request: Request):
    from .db import run_db
    from .async_storage import download_response
    descriptor = await run_db(request, prepare_download, asset_id, request)
    return await download_response(request, descriptor, lambda db: prepare_download(db, asset_id, request, False))


@router.get("/assets/{asset_id}/preview", dependencies=[Depends(media_slot)])
def preview(asset_id: str, request: Request, db=Depends(get_db), user=Depends(optional_user)):
    enforce_scope(request, "content:read")
    a = fetch(db, Asset, asset_id)
    v = fetch(db, Version, a.version_id)
    settings = request.app.state.settings
    full = can_read_content(db, user, v, settings)
    ds = db.get(Dataset, v.dataset_id)
    require(ds.status != "blocked", "数据集已封禁，不能预览", 403)
    reviewer = (v.status == "pending_review" and review_publication(db, user, ds)
                and not getattr(request.state, "service_identity", False))
    if reviewer and ds.visibility == "public" and settings.require_admin_mfa:
        reviewer = request.state.mfa
    require(full or reviewer or (a.preview_allowed and v.status == "published" and can_view_version(db, user, v, settings)),
            "该样例未开放预览", 403)
    require(a.status == "clean", "文件未通过检查", 409)
    from .evidence_access import is_truth
    from .policy import manage_dataset
    require(not is_truth(db,a) or manage_dataset(db,user,ds),'评测真值仅通过独立测评核验入口访问',403)
    ext = a.filename.rsplit(".", 1)[-1].lower()
    if ext in {"txt", "csv", "json", "jsonl", "md"}:
        data = b"".join(request.app.state.storage.chunks(a.object_key, 0, min(a.size, 65536) - 1))
        result = {"kind": "text", "content": "\n".join(data.decode("utf-8", errors="replace").splitlines()[:30]),
                  "truncated": True}
    elif ext in {"png", "jpg", "jpeg", "webp"}:
        require(a.size <= 20 * 1024**2, "此图像过大，请使用授权下载", 413)
        from .preview_cache import thumbnail
        result = {"kind": "image", "content": "data:image/jpeg;base64," + base64.b64encode(thumbnail(request,a)).decode()}
    else:
        result = {"kind": "metadata", "content": "此格式提供授权下载；在线预览尚未开放。", "size": a.size}
    audit(db, user, "asset.preview", a.id, db.get(Dataset, v.dataset_id).space_id)
    db.commit()
    return result
