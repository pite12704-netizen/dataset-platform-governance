"""【学习标注：本地批量接入】冻结目录清单，流式计算哈希，不复制或修改原图。"""
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

from .exporter import MAX_IMAGE, SourceError, canonical

FORMATS = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_FILES = 100000


def linked(path):
    """Windows junction/reparse point 和符号链接都不作为普通原图读取。"""
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) &
                                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))


def checked_path(root, relative):
    """只允许显式输入目录下的普通文件，拒绝绝对路径、上跳和链接。"""
    if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
            or any(ord(char) < 32 for char in relative)):
        raise SourceError('清单文件路径超出导出目录或文件不存在')
    parts = relative.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise SourceError('清单文件路径超出导出目录或文件不存在')
    path = root
    try:
        for part in parts:
            path = path / part
            if linked(path):
                raise SourceError('本地清单不能读取符号链接或目录联接')
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise SourceError('清单文件路径超出导出目录或文件不存在')
    except OSError:
        raise SourceError('清单文件路径超出导出目录或文件不存在') from None
    return path


def signature(info):
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino]


def freeze(root, destination, skipped_path):
    """目录枚举只保留本层排序；图片名单冻结后续传不自动吸收新增文件。"""
    count = skipped = 0
    temporary = destination.with_suffix('.tmp')

    def unreadable(_):
        raise SourceError('有子目录无法读取，请检查权限后使用新的输出目录')

    with temporary.open('w', encoding='utf-8') as output, skipped_path.open('w', encoding='utf-8') as ignored:
        for directory, directories, filenames in os.walk(root, followlinks=False, onerror=unreadable):
            directories.sort()
            for name in directories:
                if linked(Path(directory) / name):
                    raise SourceError('输入目录包含链接或目录联接，请移除链接后重新准备清单')
            for name in sorted(filenames):
                path = Path(directory) / name
                if linked(path) or not path.is_file():
                    raise SourceError('输入目录包含链接或非普通文件，请检查后重新准备清单')
                relative = path.relative_to(root).as_posix()
                if path.suffix.lower() not in FORMATS:
                    ignored.write(canonical({'local_path': relative, 'reason': 'unsupported_format'}) + '\n')
                    skipped += 1
                    continue
                checked_path(root, relative)
                count += 1
                if count > MAX_FILES:
                    raise SourceError('单次最多 100000 张图片，请按来源目录分批准备')
                output.write(canonical({'local_path': relative, 'signature': signature(path.stat())}) + '\n')
    if not count:
        raise SourceError('输入目录没有 JPG/JPEG/PNG/WebP 图片')
    temporary.replace(destination)
    return count, skipped


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_local(input_dir, output, source_id, source, license, progress=print):
    """生成兼容现有批次接口的清单；输出必须在原图目录以外。"""
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', source_id):
        raise SourceError('source_id 只允许 1～100 位字母、数字、点、下划线或短横线')
    if not source.strip() or len(source) > 2000 or not license.strip() or len(license) > 1000:
        raise SourceError('请填写数据来源和使用许可，来源最多 2000 字、许可最多 1000 字')
    root = Path(input_dir).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if not root.is_dir():
        raise SourceError('输入目录不存在或不是目录')
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise SourceError('输出目录与原图目录不能互相包含，请使用独立目录')
    output.mkdir(parents=True, exist_ok=True)
    marker = output / 'local-snapshot.json'
    snapshot = output / 'local-files.jsonl'
    fingerprint = hashlib.sha256(canonical({'root': str(root), 'source_id': source_id,
                                          'source': source, 'license': license}).encode()).hexdigest()
    # SQLite 只作客户本地的跨进程锁；不连接平台或客户的业务数据库。
    state = sqlite3.connect(output / 'local-prepare.sqlite', timeout=1)
    try:
        try:
            state.execute('PRAGMA locking_mode=EXCLUSIVE')
            state.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            raise SourceError('另一个进程正在准备同一输出目录，请等待它结束') from None
        state.execute('CREATE TABLE IF NOT EXISTS source_hashes (path TEXT PRIMARY KEY,sha256 TEXT NOT NULL)')
        if marker.exists():
            frozen = json.loads(marker.read_text(encoding='utf-8'))
            if frozen['config_sha256'] != fingerprint or file_digest(snapshot) != frozen['snapshot_sha256']:
                raise SourceError('原目录、来源配置或冻结清单已变化，请使用新的输出目录')
        else:
            # 不覆盖 MySQL 导出或其他已经存在的导入清单。
            if (output / 'summary.json').exists() or (output / 'manifest.jsonl').exists():
                raise SourceError('输出目录已有其他导入结果，请使用新的输出目录')
            count, skipped = freeze(root, snapshot, output / 'skipped.jsonl')
            frozen = {'config_sha256': fingerprint, 'snapshot_sha256': file_digest(snapshot),
                      'expected_files': count, 'skipped_files': skipped}
            temporary = marker.with_suffix('.tmp')
            temporary.write_text(canonical(frozen), encoding='utf-8')
            temporary.replace(marker)
        summary = {'source_type': 'local-directory', 'source_id': source_id, 'complete': False,
                   'files': 0, 'bytes': 0, 'expected_files': frozen['expected_files'],
                   'skipped_files': frozen['skipped_files'], 'failed': 0}
        # 开始重算就清除旧成功标记；中断不会继续宣称本轮准备成功。
        (output / 'summary.json').write_text(canonical(summary), encoding='utf-8')
        manifest_tmp = output / 'manifest.tmp'
        manifest_hash = hashlib.sha256()
        with snapshot.open(encoding='utf-8') as records, manifest_tmp.open('wb') as manifest, \
                (output / 'errors.json').open('w', encoding='utf-8') as errors:
            errors.write('[')
            for line in records:
                row = json.loads(line)
                try:
                    path = checked_path(root, row['local_path'])
                    before = path.stat()
                    if signature(before) != row['signature']:
                        raise SourceError('文件自冻结清单后发生变化，请恢复原图或使用新输出目录')
                    if not 0 < before.st_size <= MAX_IMAGE:
                        raise SourceError('图片为空或超过 20MiB 接收上限')
                    if len(path.name) > 240:
                        raise SourceError('图片文件名超过 240 字符')
                    sha = file_digest(path)
                    if signature(path.stat()) != row['signature']:
                        raise SourceError('计算哈希期间图片发生变化')
                    relative = row['local_path']
                    prior=state.execute('SELECT sha256 FROM source_hashes WHERE path=?',(relative,)).fetchone()
                    if prior and prior[0]!=sha:
                        raise SourceError('文件自冻结清单后发生变化，请恢复原图或使用新输出目录')
                    state.execute('INSERT OR IGNORE INTO source_hashes VALUES (?,?)',(relative,sha))
                    external_id = relative if len(relative) <= 200 else 'path-' + hashlib.sha256(relative.encode()).hexdigest()
                    item = {'external_id': external_id, 'filename': path.name, 'local_path': relative,
                            'size': before.st_size, 'sha256': sha, 'source_revision': 'sha256:' + sha,
                            'source_group': '', 'source_annotation': {}, 'source': source, 'license': license}
                    raw = (canonical(item) + '\n').encode()
                    manifest.write(raw)
                    manifest_hash.update(raw)
                    summary['files'] += 1
                    summary['bytes'] += before.st_size
                except (OSError, SourceError) as exc:
                    if summary['failed']:
                        errors.write(',')
                    errors.write(canonical({'local_path': row['local_path'],
                                            'error': str(exc) if isinstance(exc, SourceError) else type(exc).__name__}))
                    summary['failed'] += 1
                done = summary['files'] + summary['failed']
                if done % 100 == 0 or done == frozen['expected_files']:
                    progress(f"准备清单 {done}/{frozen['expected_files']} · 失败 {summary['failed']}")
            errors.write(']')
        manifest_tmp.replace(output / 'manifest.jsonl')
        summary.update(manifest_sha256=manifest_hash.hexdigest(),
                       complete=summary['failed'] == 0 and summary['files'] == frozen['expected_files'])
        temporary = output / 'summary.tmp'
        temporary.write_text(canonical(summary), encoding='utf-8')
        state.commit() # 持久化内容基线；EXCLUSIVE 模式在关闭连接前继续保护结果发布。
        temporary.replace(output / 'summary.json')
        return summary
    finally:
        state.rollback()
        state.close()
