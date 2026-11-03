"""【学习标注：异步文件网关】内部签名读取 S3；签名和对象地址不返回浏览器。

下载先完成鉴权，再打开对象、核对范围、再次鉴权，最后逐块输出。每进程有界并发，
客户端背压使读取自然暂停。同步 boto3 保留给上传事务和 Celery；本地文件用独立线程额度。
"""
import anyio
import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse


class ObjectStream:
    def __init__(self, expected, *, context=None, response=None, client=None, iterator=None, limiter=None):
        self.expected = expected
        self.context, self.response, self.client = context, response, client
        self.iterator, self.limiter, self.closed = iterator, limiter, False

    async def chunks(self):
        received = 0
        if self.response is not None:
            async for chunk in self.response.aiter_raw(chunk_size=1024 * 1024):
                received += len(chunk)
                if received > self.expected:
                    raise OSError('Object length mismatch')
                yield chunk
        elif self.iterator is not None:
            sentinel = object()
            while True:
                chunk = await anyio.to_thread.run_sync(lambda: next(self.iterator, sentinel), limiter=self.limiter)
                if chunk is sentinel:
                    break
                received += len(chunk)
                if received > self.expected:
                    raise OSError('Object length mismatch')
                yield chunk
        if received != self.expected:
            raise OSError('Object stream incomplete')

    async def close(self):
        if self.closed:
            return
        self.closed = True
        with anyio.CancelScope(shield=True):
            try:
                if self.context is not None:
                    await self.context.__aexit__(None, None, None)
                if self.iterator is not None:
                    await anyio.to_thread.run_sync(self.iterator.close, limiter=self.limiter)
            finally:
                if self.client is not None:
                    await self.client.aclose()


class AsyncObjectReader:
    def __init__(self, storage, settings):
        self.storage, self.settings = storage, settings
        self.client = None
        self.file_limiter = anyio.CapacityLimiter(settings.download_max_inflight)

    async def start(self):
        if self.settings.storage_backend == 's3':
            self.client = self.make_client()

    def make_client(self):
        return httpx.AsyncClient(trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(60, connect=10, pool=1),
            limits=httpx.Limits(max_connections=self.settings.download_max_inflight,
                                max_keepalive_connections=self.settings.download_max_inflight))

    async def open(self, key, start, end):
        expected = max(0, end - start + 1)
        if self.settings.storage_backend != 's3':
            iterator = self.storage.chunks(key, start, end) if expected else iter(())
            # 普通空迭代器没有 close；空文件直接返回不含资源的流。
            return ObjectStream(expected, iterator=iterator if expected else None, limiter=self.file_limiter)
        params = {'Bucket': self.storage.bucket, 'Key': key}
        headers = {'Accept-Encoding': 'identity'}
        if expected:
            params['Range'] = headers['Range'] = f'bytes={start}-{end}'
        # 静态凭据签名不执行网络访问；单独线程保护 boto3 的同步边界。
        url = await anyio.to_thread.run_sync(lambda: self.storage.client.generate_presigned_url(
            'get_object', Params=params, ExpiresIn=60), limiter=self.file_limiter)
        client = self.client if self.client is not None else self.make_client()
        context = client.stream('GET', url, headers=headers)
        response = None
        try:
            response = await context.__aenter__()
            valid = response.status_code == (206 if expected else 200)
            valid = valid and response.headers.get('Content-Length') == str(expected)
            if expected:
                valid = valid and response.headers.get('Content-Range', '').startswith(f'bytes {start}-{end}/')
            if not valid:
                raise HTTPException(503, '对象存储响应不完整，请稍后重试', headers={'Retry-After': '1'})
            return ObjectStream(expected, context=context, response=response,
                                client=client if self.client is None else None)
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                if response is not None:
                    await context.__aexit__(None, None, None)
                if self.client is None:
                    await client.aclose()
            if isinstance(exc, httpx.HTTPError):
                raise HTTPException(503, '对象存储暂不可用，请稍后重试', headers={'Retry-After': '1'}) from None
            raise

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


class ManagedDownloadResponse(StreamingResponse):
    def __init__(self, stream, limiter, borrower, **kwargs):
        super().__init__(stream.chunks(), **kwargs)
        self.stream, self.limiter, self.borrower = stream, limiter, borrower

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                try:
                    with anyio.CancelScope(shield=True):
                        await self.body_iterator.aclose()
                finally:
                    await self.stream.close()
            finally:
                self.limiter.release_on_behalf_of(self.borrower)


async def download_response(request, descriptor, revalidate):
    from .db import run_db
    limiter, borrower = request.app.state.download_limiter, object()
    try:
        limiter.acquire_on_behalf_of_nowait(borrower)
    except anyio.WouldBlock:
        raise HTTPException(503, '下载并发已满，请稍后重试', headers={'Retry-After': '1'}) from None
    stream = None
    try:
        stream = await request.app.state.async_storage.open(descriptor['key'], descriptor['start'], descriptor['end'])
        await run_db(request, revalidate)
        return ManagedDownloadResponse(stream, limiter, borrower,
            status_code=descriptor['status_code'], media_type=descriptor.get('media_type', 'application/octet-stream'),
            headers=descriptor['headers'])
    except BaseException:
        try:
            if stream is not None:
                await stream.close()
        finally:
            limiter.release_on_behalf_of(borrower)
        raise
