"""只持有批次范围凭据；以服务器 Upload/Asset 状态恢复上传，不在磁盘保存令牌。"""
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class IngestFailure(Exception):
    pass


class PlatformClient:
    def __init__(self, base_url, token, ca_file=None, transport=None):
        parsed = urlsplit(base_url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}
                or parsed.scheme not in {'https', 'http'}
                or parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('平台须使用 HTTPS 地址；仅本机测试允许 HTTP')
        self.client = httpx.Client(base_url=base_url.rstrip('/') + '/api/v1/',
            headers={'Authorization': 'Bearer ' + token}, verify=ca_file or True,
            timeout=httpx.Timeout(60, connect=10), follow_redirects=False, transport=transport)

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        for attempt in range(6):
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 5:
                    raise IngestFailure('网络连接失败；保留服务器进度，重新运行可续传') from None
                time.sleep(min(2**attempt, 10)); continue
            if response.status_code in {408, 429, 502, 503, 504} and attempt < 5:
                retry = response.headers.get('Retry-After', '')
                time.sleep(min(60, max(1, int(retry))) if retry.isdigit() else min(2**attempt, 10)); continue
            if response.status_code >= 300:
                if response.status_code == 401:
                    raise IngestFailure('批次凭据已失效；在平台轮换凭据后重新运行续传')
                raise IngestFailure(f'平台拒绝请求（HTTP {response.status_code}）；请查看导入批次明细')
            return response.json()
        raise IngestFailure('请求重试次数已达上限')


def local_file(root, item):
    from .local_source import checked_path
    from .exporter import SourceError
    try:
        path = checked_path(root, item['local_path'])
    except SourceError as exc:
        raise IngestFailure(str(exc)) from None
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024**2), b''):
            digest.update(chunk)
    if path.stat().st_size != item['size'] or digest.hexdigest() != item['sha256']:
        raise IngestFailure('本地图片与导出清单不一致，请恢复原始缓存文件')
    return path


def send_file(client, batch_id, item, local):
    upload = client.request('POST', f'imports/{batch_id}/items/{item["id"]}/upload')
    if upload['status'] == 'completed':
        return
    upload_id, part_size = upload['id'], upload['part_size']
    digest = hashlib.sha256()
    size = 0
    with local.open('rb') as source:
        number = 0
        while chunk := source.read(part_size):
            size += len(chunk)
            if size > item['size']:
                raise IngestFailure('上传期间本地图片发生变化，请恢复原图后续传')
            digest.update(chunk)
            number += 1
            stored = upload['parts'].get(str(number))
            # ETag 不是通用内容哈希。旧服务未记录分片 SHA 时保守重传当前文件的该片。
            if stored and stored['size'] == len(chunk) and stored.get('sha256') == hashlib.sha256(chunk).hexdigest():
                continue
            client.request('PUT', f'uploads/{upload_id}/parts/{number}', content=chunk,
                           headers={'Content-Type': 'application/octet-stream'})
    if size != item['size'] or digest.hexdigest() != item['sha256']:
        raise IngestFailure('上传期间本地图片发生变化，请恢复原图后续传')
    client.request('POST', f'uploads/{upload_id}/complete')


def upload(manifest_path, batch_id, client, wait_seconds=600, retry_scans=False, progress=print, input_dir=None):
    manifest_path = Path(manifest_path).resolve(); root = manifest_path.parent
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    if not summary.get('complete') or not summary.get('files'):
        raise IngestFailure('导出尚不完整；请先修复 errors.json 中的来源记录并重新导出')
    if summary.get('source_type') == 'local-directory':
        if input_dir is None:
            raise IngestFailure('本地目录导入须用 --input-dir 显式指定原图目录')
        root = Path(input_dir).expanduser().resolve()
        if not root.is_dir():
            raise IngestFailure('本地原图目录不存在')
    elif input_dir is not None:
        raise IngestFailure('--input-dir 只适用于 prepare-local 生成的本地目录清单')
    digest = hashlib.sha256()
    with manifest_path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            digest.update(chunk)
    if digest.hexdigest() != summary['manifest_sha256']:
        raise IngestFailure('导出清单已变化，请重新导出或使用原清单')
    batch = client.request('GET', f'imports/{batch_id}')
    if (batch['source_id'], batch['expected_files'], batch['expected_bytes']) != (summary['source_id'], summary['files'], summary['bytes']):
        raise IngestFailure('平台批次的来源、文件数或字节数与导出摘要不同')
    if batch['status'] == 'cancelled':
        raise IngestFailure('平台导入已取消')
    if not batch['sealed']:
        with manifest_path.open(encoding='utf-8') as records:
            page = []
            for line in records:
                page.append(json.loads(line))
                if len(page) == 100:
                    send_page(client, batch_id, root, page, progress); page = []
            if page:
                send_page(client, batch_id, root, page, progress)
        batch = client.request('POST', f'imports/{batch_id}/finalize')
    if retry_scans and batch['status'] == 'partial_failed':
        offset = 0
        while True:
            page = client.request('GET', f'imports/{batch_id}/items?offset={offset}&limit=100')
            for item in page['items']:
                if item['status'] == 'failed':
                    client.request('POST', f'imports/{batch_id}/items/{item["id"]}/retry-scan')
            if not page['has_more']:
                break
            offset += len(page['items'])
    deadline = time.monotonic() + wait_seconds
    while True:
        batch = client.request('GET', f'imports/{batch_id}')
        progress(json.dumps({'status': batch['status'], **batch['counts']}, ensure_ascii=False))
        if batch['status'] == 'completed':
            return batch
        if batch['status'] in {'partial_failed', 'cancelled'}:
            raise IngestFailure('批次存在失败或被取消；请在平台明细处理，未标记为成功')
        if time.monotonic() >= deadline:
            raise IngestFailure('文件已上传，后台检查尚未完成；稍后重新运行即可查询续接')
        time.sleep(3)


def send_page(client, batch_id, root, page, progress):
    files = {item['external_id']: local_file(root, item) for item in page}
    registered = client.request('POST', f'imports/{batch_id}/items',
        json={'items': [{k: v for k, v in item.items() if k != 'local_path'} for item in page]})
    for item in registered['items']:
        send_file(client, batch_id, item, files[item['external_id']])
        progress('已传输来源记录：' + item['external_id'])
