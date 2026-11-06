"""SQLAlchemy checkpointer for this fixed graph, shared by MySQL and isolated SQLite.

Each saver method owns a short session, including calls made by LangGraph's writer
thread. A renewable, fenced lease serializes a workflow across API processes.
Only LastValue channels are used; the latest eight complete snapshots suffice.
This adapter deliberately provides no client-selected namespaces or time travel.
"""
import hashlib
import json
import math
from datetime import timedelta

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from langgraph.types import Interrupt
from sqlalchemy import delete, func, or_, select, update

from .common import require
from .db import now
from .models import AgentGraphCheckpoint, AgentGraphWorkflow, AgentGraphWrite

CHECKPOINT_BYTES = 32768
WRITE_BYTES = 8192
KEEP_CHECKPOINTS = 8


def pack(value, depth=0):
    """Closed type set: no module/class loading, pickle, or general object constructors."""
    require(depth <= 24, "流程状态嵌套超限", 413)
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) in {list, tuple}:
        return {"t": "tuple" if type(value) is tuple else "list", "v": [pack(x, depth+1) for x in value]}
    if type(value) is dict and all(type(k) is str for k in value):
        return {"t": "dict", "v": {k: pack(v, depth+1) for k, v in value.items()}}
    if type(value) is Interrupt and value.response_schema is None:
        return {"t": "interrupt", "id": value.id, "v": pack(value.value, depth+1)}
    if isinstance(value, Exception):
        # Raw exception messages can contain SQL, paths or provider response bodies.
        return {"t": "error", "v": "workflow_step_failed"}
    raise ValueError("Unsupported graph checkpoint type: " + type(value).__name__)


def unpack(value, depth=0):
    require(depth <= 24, "流程状态嵌套超限", 413)
    if value is None or type(value) in {str, bool, int, float}:
        return value
    require(type(value) is dict, "流程检查点格式无效", 409)
    kind = value.get("t")
    if kind == "dict":
        return {k: unpack(v, depth+1) for k, v in value["v"].items()}
    if kind in {"list", "tuple"}:
        items = [unpack(x, depth+1) for x in value["v"]]
        return tuple(items) if kind == "tuple" else items
    if kind == "interrupt":
        return Interrupt(unpack(value["v"], depth+1), id=value["id"])
    if kind == "error":
        return "workflow_step_failed"
    raise ValueError("Unknown graph checkpoint tag")


def bounded(value, limit):
    packed = pack(value)
    require(len(json.dumps(packed, ensure_ascii=False, allow_nan=False).encode()) <= limit,
            "流程检查点大小超限，请缩小操作范围", 413)
    return packed


class SafeGraphSerializer:
    def dumps_typed(self, value):
        return "platform-json-v1", json.dumps(bounded(value, CHECKPOINT_BYTES), ensure_ascii=False).encode()

    def loads_typed(self, value):
        kind, body = value
        require(kind == "platform-json-v1" and len(body) <= CHECKPOINT_BYTES, "流程序列化格式无效", 409)
        return unpack(json.loads(body))


def thread_config(identifier, checkpoint_id=None):
    config = {"configurable": {"thread_id": identifier, "checkpoint_ns": ""}}
    if checkpoint_id:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    return config


def clear_checkpoints(db, workflow_id):
    db.execute(delete(AgentGraphWrite).where(AgentGraphWrite.workflow_id == workflow_id))
    db.execute(delete(AgentGraphCheckpoint).where(AgentGraphCheckpoint.workflow_id == workflow_id))


class SQLGraphSaver(BaseCheckpointSaver[int]):
    def __init__(self, database, workflow_id, *, token=None, lease_seconds=120):
        super().__init__(serde=SafeGraphSerializer())
        self.database, self.workflow_id = database, workflow_id
        self.token, self.lease_seconds = token, lease_seconds

    def check_config(self, config):
        fields = config.get("configurable", {})
        require(fields.get("thread_id") == self.workflow_id and not fields.get("checkpoint_ns"),
                "流程检查点归属不一致", 409)

    def guard(self, db):
        require(self.token is not None, "只读检查点禁止修改", 409)
        changed = db.execute(update(AgentGraphWorkflow).where(
            AgentGraphWorkflow.id == self.workflow_id, AgentGraphWorkflow.lease_token == self.token,
            AgentGraphWorkflow.lease_until > now(),
            AgentGraphWorkflow.status.not_in({"cancelled", "invalidated", "redacted"})
        ).values(lease_until=now()+timedelta(seconds=self.lease_seconds)))
        require(changed.rowcount == 1, "流程执行租约已变化，请刷新后恢复", 409)

    def acquire(self, token):
        with self.database.Session() as db:
            changed = db.execute(update(AgentGraphWorkflow).where(
                AgentGraphWorkflow.id == self.workflow_id,
                AgentGraphWorkflow.status.not_in({"cancelled", "invalidated", "redacted"}),
                or_(AgentGraphWorkflow.lease_token.is_(None), AgentGraphWorkflow.lease_until <= now())
            ).values(lease_token=token, lease_until=now()+timedelta(seconds=self.lease_seconds), failure_code=None))
            require(changed.rowcount == 1, "流程正在执行或已失效，请稍后查询状态", 409)
            db.commit()
        self.token = token

    def release(self, status=None, failure_code=None):
        with self.database.Session() as db:
            values = {"lease_token": None, "lease_until": None, "failure_code": failure_code}
            if status:
                values["status"] = status
            db.execute(update(AgentGraphWorkflow).where(AgentGraphWorkflow.id == self.workflow_id,
                AgentGraphWorkflow.lease_token == self.token).values(**values))
            db.commit()

    def get_tuple(self, config):
        self.check_config(config)
        with self.database.Session() as db:
            query = select(AgentGraphCheckpoint).where(AgentGraphCheckpoint.workflow_id == self.workflow_id)
            identifier = config["configurable"].get("checkpoint_id")
            if identifier:
                query = query.where(AgentGraphCheckpoint.checkpoint_id == identifier)
            row = db.scalar(query.order_by(AgentGraphCheckpoint.checkpoint_id.desc()).limit(1))
            if row is None:
                return None
            writes = db.scalars(select(AgentGraphWrite).where(AgentGraphWrite.workflow_id == self.workflow_id,
                AgentGraphWrite.checkpoint_id == row.checkpoint_id).order_by(
                    AgentGraphWrite.task_path, AgentGraphWrite.task_id, AgentGraphWrite.write_index)).all()
            return CheckpointTuple(thread_config(self.workflow_id, row.checkpoint_id), unpack(row.checkpoint),
                unpack(row.checkpoint_metadata), thread_config(self.workflow_id, row.parent_id) if row.parent_id else None,
                [(v.task_id, v.channel, unpack(v.value)) for v in writes])

    def put(self, config, checkpoint, metadata, new_versions):
        self.check_config(config)
        payload, meta = bounded(checkpoint, CHECKPOINT_BYTES), bounded(metadata, WRITE_BYTES)
        identifier = checkpoint["id"]
        require(isinstance(identifier, str) and len(identifier) <= 64, "检查点标识无效", 409)
        with self.database.Session() as db:
            self.guard(db)
            existing = db.scalar(select(AgentGraphCheckpoint).where(
                AgentGraphCheckpoint.workflow_id == self.workflow_id, AgentGraphCheckpoint.checkpoint_id == identifier))
            if existing is None:
                require(db.scalar(select(func.count()).select_from(AgentGraphCheckpoint).where(
                    AgentGraphCheckpoint.workflow_id == self.workflow_id)) < 64, "流程检查点数量超限", 413)
                db.add(AgentGraphCheckpoint(workflow_id=self.workflow_id, checkpoint_id=identifier,
                    parent_id=config["configurable"].get("checkpoint_id"), checkpoint=payload, checkpoint_metadata=meta))
            else:
                require(existing.checkpoint == payload, "同一检查点内容不一致", 409)
            db.commit()
        return thread_config(self.workflow_id, identifier)

    def put_writes(self, config, writes, task_id, task_path=""):
        self.check_config(config)
        require(len(task_id) <= 64 and len(task_path) <= 200 and len(writes) <= 64, "流程写入数量超限", 413)
        identifier = config["configurable"]["checkpoint_id"]
        with self.database.Session() as db:
            self.guard(db)
            count = db.scalar(select(func.count()).select_from(AgentGraphWrite).where(
                AgentGraphWrite.workflow_id == self.workflow_id))
            for index, (channel, value) in enumerate(writes):
                require(len(channel) <= 100, "流程通道标识超限", 413)
                slot = WRITES_IDX_MAP.get(channel, index)
                row_id = hashlib.sha256(f"{self.workflow_id}:{identifier}:{task_id}:{slot}".encode()).hexdigest()[:32]
                existing = db.get(AgentGraphWrite, row_id)
                packed = bounded(value, WRITE_BYTES)
                if existing is not None:
                    if slot < 0:
                        existing.value, existing.channel, existing.task_path = packed, channel, task_path
                else:
                    count += 1
                    require(count <= 256, "流程待提交写入超限", 413)
                    db.add(AgentGraphWrite(id=row_id, workflow_id=self.workflow_id, checkpoint_id=identifier,
                        task_id=task_id, task_path=task_path, write_index=slot, channel=channel, value=packed))
            db.commit()

    def prune_history(self):
        # Safe for this graph's full LastValue snapshots; DeltaChannel is unsupported.
        with self.database.Session() as db:
            self.guard(db)
            keep = list(db.scalars(select(AgentGraphCheckpoint.checkpoint_id).where(
                AgentGraphCheckpoint.workflow_id == self.workflow_id).order_by(
                    AgentGraphCheckpoint.checkpoint_id.desc()).limit(KEEP_CHECKPOINTS)))
            if keep:
                db.execute(delete(AgentGraphWrite).where(AgentGraphWrite.workflow_id == self.workflow_id,
                    AgentGraphWrite.checkpoint_id.not_in(keep)))
                db.execute(delete(AgentGraphCheckpoint).where(AgentGraphCheckpoint.workflow_id == self.workflow_id,
                    AgentGraphCheckpoint.checkpoint_id.not_in(keep)))
            db.commit()
