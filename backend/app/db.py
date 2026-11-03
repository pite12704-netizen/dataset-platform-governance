"""【学习标注：数据库基础】定义 ORM 基类、连接池、会话生命周期、主键和统一时间。"""
from datetime import datetime, timezone
from uuid import uuid4
import asyncio
import hashlib

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from sqlalchemy.pool import StaticPool
from fastapi import Request
from starlette.concurrency import run_in_threadpool


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def uid():
    return uuid4().hex


class Base(DeclarativeBase):
    pass


@event.listens_for(Session,'before_flush')
def bound_job_state(session,flush_context,instances):
    from .job_payloads import compact_pending
    compact_pending(session)


class Database:
    def __init__(self, url, settings=None):
        self.backup_metadata_max_bytes = settings.backup_metadata_max_bytes if settings else 128*1024**2
        self.mfa_key_ids = [hashlib.sha256(key.encode()).hexdigest() for key in settings.mfa_encryption_keys] if settings else []
        args = {"pool_pre_ping": True}
        if url.startswith("mysql"):
            args.update(pool_size=settings.database_pool_size if settings else 10,
                        max_overflow=settings.database_max_overflow if settings else 10,
                        pool_recycle=settings.database_pool_recycle if settings else 1800,
                        pool_timeout=settings.database_pool_timeout if settings else 5,
                        connect_args={"connect_timeout": settings.database_connect_timeout if settings else 10,
                                      "read_timeout": settings.database_read_timeout if settings else 30,
                                      "write_timeout": settings.database_write_timeout if settings else 30},
                        isolation_level="READ COMMITTED")
        if url.startswith("sqlite"):
            args["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                args["poolclass"] = StaticPool
        self.engine = create_engine(url, **args)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def sqlite_config(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
        self.Session = sessionmaker(self.engine, expire_on_commit=False)


def get_db(request: Request):
    with request.app.state.db.Session() as db:
        yield db


class AsyncDatabase:
    """【学习标注：原生异步数据库】承接 Agent、目录与限流身份的短 SQL 阶段。

    run_sync 的 greenlet 会在 aiomysql I/O 处让出事件循环，不占 API 工作线程。
    回调内禁止 boto3、HTTP、密码计算等阻塞操作；每次调用独立 AsyncSession。
    工人进程和迁移仍使用 Database，同一份连接配置内部转换驱动名。
    """
    def __init__(self, url, settings):
        parsed = make_url(url)
        args = {"pool_pre_ping": True}
        if parsed.get_backend_name() == "mysql":
            parsed = parsed.set(drivername="mysql+aiomysql")
            args.update(pool_size=settings.database_async_pool_size,
                        max_overflow=settings.database_async_max_overflow,
                        pool_timeout=settings.database_pool_timeout,
                        pool_recycle=settings.database_pool_recycle,
                        connect_args={"connect_timeout": settings.database_connect_timeout},
                        isolation_level="READ COMMITTED")
        elif parsed.get_backend_name() == "sqlite":
            parsed = parsed.set(drivername="sqlite+aiosqlite")
            args["connect_args"] = {"timeout": 30}
        else:
            raise ValueError("Unsupported async database backend")
        self.engine = create_async_engine(parsed, **args)
        if parsed.get_backend_name() == "sqlite":
            @event.listens_for(self.engine.sync_engine, "connect")
            def sqlite_config(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.timeout = settings.database_read_timeout + settings.database_pool_timeout

    async def run(self, operation, *args):
        try:
            async with asyncio.timeout(self.timeout):
                async with self.Session() as db:
                    return await db.run_sync(lambda session: operation(session, *args))
        except TimeoutError:
            # 超时不重放事务；提交过程中断时，客户端须按 request_id 查询实际结果。
            raise OperationalError(None, None, TimeoutError("Database phase deadline")) from None


async def run_sql(request: Request, operation, *args):
    """纯 SQL 回调返回普通值；SQLite 内存测试复用唯一同步引擎以免产生另一数据库。"""
    database = getattr(request.app.state, "async_db", None)
    if database is not None:
        return await database.run(operation, *args)
    return await run_db(request, operation, *args)


async def run_db(request: Request, operation, *args):
    """【学习标注：异步边界】在线程内创建、使用并关闭会话，不把 ORM 对象传回协程。

    operation 是同步业务函数，接收 db 和普通参数，返回普通数据或 Response。
    不自动提交或重试；事务是否提交仍由业务函数决定。异常时 close 回滚。
    AnyIO 默认等待线程结束，避免取消请求后把仍在使用的会话提前关闭。
    """
    def execute():
        with request.app.state.db.Session() as db:
            return operation(db, *args)
    return await run_in_threadpool(execute)
