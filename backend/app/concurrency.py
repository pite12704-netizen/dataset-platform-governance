"""【学习标注：工作量隔离】在同步工作进入线程池前限制媒体和 MCP 并发。"""
from contextlib import asynccontextmanager

import anyio
from fastapi import HTTPException, Request


@asynccontextmanager
async def admission(limiter, message):
    borrower = object()
    try:
        limiter.acquire_on_behalf_of_nowait(borrower)
    except anyio.WouldBlock:
        raise HTTPException(503, message, headers={'Retry-After': '1'}) from None
    try:
        yield
    finally:
        limiter.release_on_behalf_of(borrower)


async def media_slot(request: Request):
    async with admission(request.app.state.media_limiter, '图像预览并发已满，请稍后重试'):
        yield
