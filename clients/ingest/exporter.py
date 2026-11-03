"""只读字段映射、源快照、条件对象读取及可恢复的逐文件缓存。"""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import MetaData, Table, create_engine, select

MAX_IMAGE = 20 * 1024**2


class SourceError(ValueError):
    """可安全展示的本机配置或来源校验错误，不包含 SDK 异常内容。"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def env_value(name):
    value = os.environ.get(name or '')
    secret_file = os.environ.get((name or '') + '_FILE')
    if secret_file:
        value = Path(secret_file).read_text(encoding='utf-8').strip()
    if not value:
        raise SourceError('缺少配置指定的环境变量')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise SourceError('表和字段必须使用简单 SQL 标识符')
    return value


def object_key(value, storage):
    """只解析配置允许的 URL 前缀；所有字节都通过指定 S3 端点读取。"""
    prefix = storage.get('url_prefix')
    if not prefix:
        raise SourceError('URL 列必须配置明确的 url_prefix')
    url, base = urlsplit(str(value)), urlsplit(prefix)
    if url.username or url.password or url.fragment or base.query or base.fragment:
        raise SourceError('不允许带用户信息或片段的来源 URL')
    if (url.scheme, url.netloc) != (base.scheme, base.netloc) or not base.path.endswith('/') or not url.path.startswith(base.path):
        raise SourceError('图片 URL 不属于配置的来源前缀')
    key = unquote(url.path[len(base.path):])
    if not key or '\x00' in key:
        raise SourceError('图片对象键无效')
    return key


def metadata_snapshot(engine, config, destination):
    db = config['database']; mapping = db['columns']
    if ('key' in mapping) == ('url' in mapping):
        raise SourceError('必须且只能映射 key 或 url 一种来源列')
    names = {k: identifier(v) for k, v in mapping.items()}
    table = Table(identifier(db['table']), MetaData(), autoload_with=engine)
    if set(names.values()) - set(table.c.keys()) or set(table.primary_key.columns.keys()) != {names['id']}:
        raise SourceError('字段不存在，或 id 不是单列主键')
    query_columns = [table.c[name].label(alias) for alias, name in names.items()]
    temporary = destination.with_suffix('.tmp')
    count = 0
    # 先完成元数据快照，再读取图片，避免整个图片传输期间占用源数据库事务。
    with engine.connect() as conn, temporary.open('w', encoding='utf-8') as out:
        if engine.dialect.name == 'mysql':
            conn = conn.execution_options(isolation_level='REPEATABLE READ')
        last = db.get('start_after')
        while True:
            query = select(*query_columns).order_by(table.c[names['id']]).limit(500)
            if last is not None:
                query = query.where(table.c[names['id']] > last)
            if db.get('end_at') is not None:
                query = query.where(table.c[names['id']] <= db['end_at'])
            rows = conn.execute(query).mappings().all()
            if not rows:
                break
            for row in rows:
                key = str(row['key']) if 'key' in names else object_key(row['url'], config['storage'])
                if not key or '\x00' in key:
                    raise SourceError('对象键为空或含控制字符')
                annotation = row.get('annotation') or {}
                if isinstance(annotation, str):
                    annotation = json.loads(annotation)
                if not isinstance(annotation, dict) or len(canonical(annotation).encode()) > 4096:
                    raise SourceError('来源标签应为最多 4KiB 的 JSON 对象')
                out.write(canonical({'external_id': str(row['id']), 'key': key,
                    'version': str(row.get('version') or ''), 'revision': str(row.get('revision') or ''),
                    'group': str(row.get('group') or ''), 'annotation': annotation}) + '\n')
                count += 1
                if count > 100000:
                    raise SourceError('单个导出最多 100000 条；请用 start_after/end_at 分段导出到不同目录')
            last = rows[-1]['id']
    temporary.replace(destination)
    return count


def export(config, output, engine=None, storage=None):
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(canonical(config).encode()).hexdigest()
    marker = output / 'snapshot-config.sha256'
    snapshot = output / 'records.jsonl'
    if marker.exists() and marker.read_text().strip() != fingerprint:
        raise SourceError('导出配置已变化，请使用新的输出目录')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', config.get('source_id', '')):
        raise SourceError('source_id 无效')
    if not snapshot.exists():
        owned_engine = engine is None
        if engine is None:
            engine = create_engine(env_value(config['database']['url_env']), pool_pre_ping=True)
            if engine.dialect.name != 'mysql':
                engine.dispose(); raise SourceError('命令行导出仅支持 MySQL 来源')
        try:
            metadata_snapshot(engine, config, snapshot)
            marker.write_text(fingerprint, encoding='ascii')
        finally:
            if owned_engine:
                engine.dispose()
    elif not marker.exists():
        raise SourceError('快照缺少配置指纹，请使用新输出目录重新导出')
    cfg = config['storage']
    if storage is None:
        import boto3
        from botocore.config import Config
        endpoint = env_value(cfg['endpoint_env'])
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and cfg.get('allow_http') is True):
            raise SourceError('来源存储须用 HTTPS；内网 HTTP 必须显式 allow_http')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SourceError('来源端点不能包含凭据或查询参数')
        storage = boto3.client('s3', endpoint_url=endpoint,
            aws_access_key_id=env_value(cfg['access_key_env']), aws_secret_access_key=env_value(cfg['secret_key_env']),
            verify=cfg.get('ca_file') or True, config=Config(signature_version='s3v4',
                connect_timeout=10, read_timeout=60, retries={'max_attempts': 3}, s3={'addressing_style': 'path'}))
    cache = output / 'images'; cache.mkdir(exist_ok=True)
    if not cache.resolve().is_relative_to(output):
        raise SourceError('图片缓存目录不能链接到导出目录以外')
    state = sqlite3.connect(output / 'export-state.sqlite')
    state.execute('CREATE TABLE IF NOT EXISTS objects (external_id TEXT PRIMARY KEY, spec TEXT NOT NULL, manifest TEXT)')
    errors = []
    try:
        with snapshot.open(encoding='utf-8') as records:
            for line in records:
                row = json.loads(line); external_id = row['external_id']
                filename = hashlib.sha256(external_id.encode()).hexdigest() + Path(row['key']).suffix.lower()
                target = cache / filename
                try:
                    if target.suffix not in {'.png', '.jpg', '.jpeg', '.webp'}:
                        raise SourceError('不支持的图片格式')
                    old = state.execute('SELECT spec,manifest FROM objects WHERE external_id=?', (external_id,)).fetchone()
                    if old and old[1] and target.exists():
                        saved = json.loads(old[1]); digest = hashlib.sha256()
                        with target.open('rb') as source:
                            for chunk in iter(lambda: source.read(1024**2), b''):
                                digest.update(chunk)
                        if target.stat().st_size == saved['size'] and digest.hexdigest() == saved['sha256']:
                            continue
                    if old:
                        spec = json.loads(old[0])
                    else:
                        args = {'Bucket': cfg['bucket'], 'Key': row['key']}
                        if row['version']:
                            args['VersionId'] = row['version']
                        head = storage.head_object(**args)
                        spec = {**args, 'size': int(head['ContentLength']), 'etag': head['ETag']}
                        if head.get('VersionId') and head['VersionId'] != 'null':
                            spec['VersionId'] = head['VersionId']
                        state.execute('INSERT INTO objects VALUES (?,?,NULL)', (external_id, canonical(spec))); state.commit()
                    if not 0 < spec['size'] <= MAX_IMAGE:
                        raise SourceError('图片超过 20MiB 接收上限或为空')
                    args = {k: v for k, v in spec.items() if k in {'Bucket', 'Key', 'VersionId'}}
                    response = storage.get_object(**args, IfMatch=spec['etag'])
                    body = response['Body']; digest = hashlib.sha256(); size = 0
                    temporary = target.with_suffix(target.suffix + '.part')
                    try:
                        with temporary.open('wb') as out:
                            while chunk := body.read(1024**2):
                                size += len(chunk)
                                if size > spec['size']:
                                    raise SourceError('源图片大小已变化')
                                digest.update(chunk); out.write(chunk)
                        if size != spec['size']:
                            raise SourceError('源图片不完整')
                        temporary.replace(target)
                    finally:
                        body.close()
                        if temporary.exists():
                            temporary.unlink()
                    item = {'external_id': external_id, 'filename': filename, 'local_path': 'images/' + filename,
                        'size': size, 'sha256': digest.hexdigest(), 'source_revision': row['revision'] or str(spec.get('VersionId') or spec['etag']),
                        'source_group': row['group'], 'source_annotation': row['annotation'],
                        'source': config.get('source', ''), 'license': config.get('license', '')}
                    state.execute('UPDATE objects SET manifest=? WHERE external_id=?', (canonical(item), external_id)); state.commit()
                except Exception as exc:
                    # SDK 异常可能携带签名 URL 或端点；错误清单只保存来源 ID 和异常类型。
                    errors.append({'external_id': external_id, 'error': type(exc).__name__})
        files = size = 0; digest = hashlib.sha256()
        with (output / 'manifest.jsonl').open('wb') as out:
            for (value,) in state.execute('SELECT manifest FROM objects WHERE manifest IS NOT NULL ORDER BY external_id'):
                item = json.loads(value); raw = (canonical(item) + '\n').encode()
                out.write(raw); digest.update(raw); files += 1; size += item['size']
        summary = {'source_id': config['source_id'], 'files': files, 'bytes': size,
                   'manifest_sha256': digest.hexdigest(), 'complete': not errors, 'failed': len(errors)}
        (output / 'summary.json').write_text(canonical(summary), encoding='utf-8')
        (output / 'errors.json').write_text(canonical(errors), encoding='utf-8')
        return summary
    finally:
        state.close()
