"""Bounded per-process thumbnail cache; callers MUST authorize before accessing it.

Only immutable image bytes are cached. User permissions, URLs, sessions and API
responses are never cached. Single-flight prevents duplicate decodes of a hot image.
"""
import io
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError

from fastapi import HTTPException


class ThumbnailCache:
    def __init__(self, max_bytes=32*1024**2, ttl=600):
        self.max_bytes,self.ttl=max_bytes,ttl
        self.entries=OrderedDict();self.pending={};self.bytes=0
        self.lock=threading.Lock()

    def get_or_create(self,key,create):
        with self.lock:
            found=self.entries.pop(key,None)
            if found:
                if found[0]>time.monotonic():
                    self.entries[key]=found
                    return found[1]
                self.bytes-=len(found[1])
            future=self.pending.get(key)
            leader=future is None
            if leader:future=Future();self.pending[key]=future
        if not leader:
            try:return future.result(timeout=2)
            except TimeoutError:raise HTTPException(503,'缩略图正在生成，请稍后重试',headers={'Retry-After':'1'}) from None
        try:
            value=create()
            with self.lock:
                if len(value)<=self.max_bytes:
                    self.entries[key]=(time.monotonic()+self.ttl,value);self.bytes+=len(value)
                    while self.bytes>self.max_bytes or len(self.entries)>512:
                        _,(_,old)=self.entries.popitem(last=False);self.bytes-=len(old)
            future.set_result(value)
            return value
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:self.pending.pop(key,None)


def thumbnail(request,asset,size=(640,480),quality=75):
    from .image_reviews import read_image
    from .common import require
    require(asset.status=='clean' and 0<asset.size<=20*1024**2,'原图检查状态或容量不可预览',409)
    def create():
        from .thumbnails import read_persistent
        persisted=read_persistent(request,asset,size,quality)
        if persisted is not None:return persisted
        image=read_image(asset,request.app.state.storage)
        try:
            image.thumbnail(size);output=io.BytesIO();image.save(output,'JPEG',quality=quality)
            return output.getvalue()
        finally:image.close()
    return request.app.state.preview_cache.get_or_create(
        ('exif-jpeg-v1',asset.id,asset.sha256,*size,quality),create)
