"""Human-confirmed StateGraph orchestration, reusing the platform's business API layer.

No LLM decides whether a confirmation happened. Checkpoints hold references only;
AgentAction/Job remain the authoritative receipts. Nodes open short DB sessions;
waiting is an interrupt, never a coroutine holding a connection for minutes.
"""
from typing import TypedDict

from fastapi import HTTPException
from sqlalchemy import func, select, update

from .common import audit, fetch, public_row, require
from .db import now, uid
from .models import AgentAction, AgentGraphWorkflow
from .policy import can_read_content, manage_dataset
from .quality_contract import digest
from .security import resolve_interactive_user

GRAPH_VERSION = "confirmed-governance-v1"
INVALID = {"cancelled", "invalidated", "redacted"}


class WorkflowState(TypedDict, total=False):
    action_id: str
    version_id: str
    plan_hash: str
    approved: bool
    stage: str
    task_status: str


def register(db, action, settings):
    if settings.agent_workflow_engine != "langgraph":
        return
    # A dedicated quota sentinel avoids upgrading the action FK's shared User lock.
    # User -> action/root lock inversions would otherwise deadlock concurrent previews.
    from .capacity import lock
    lock(db, digest(['agent-graph-quota', action.user_id])[:32])
    count = db.scalar(select(func.count()).select_from(AgentGraphWorkflow).join(
        AgentAction, AgentGraphWorkflow.action_id == AgentAction.id).where(
        AgentGraphWorkflow.user_id == action.user_id, AgentAction.status == "pending",
        AgentAction.expires_at > now(), AgentGraphWorkflow.status.not_in(INVALID)))
    require(count < settings.agent_graph_max_pending, "待确认流程已满，请先处理或取消旧计划", 429)
    db.add(AgentGraphWorkflow(id=action.id, action_id=action.id, user_id=action.user_id,
        graph_version=GRAPH_VERSION, status="created"))
    db.flush()


def receipt(db, action):
    row = db.get(AgentGraphWorkflow, action.id)
    if row is None:
        return None
    status = "expired" if action.status == "pending" and action.expires_at <= now() else row.status
    return {"engine": "langgraph", "workflow_id": row.id, "graph_version": row.graph_version,
            "status": status, "failure_code": row.failure_code,
            "busy": bool(row.lease_token and row.lease_until and row.lease_until > now()),
            "recoverable": status not in INVALID | {"completed", "failed", "expired"},
            "checkpoint_storage": "platform-sql", "source": "persistent-graph-checkpoint"}


def public_action(db, action):
    value = public_row(action)
    orchestration = receipt(db, action)
    if orchestration:
        value["orchestration"] = orchestration
    return value


def _authorize(db, request, user_id, action_id, *, token=None):
    from .assistant_tools import visible_version
    user = resolve_interactive_user(request, db)
    require(user.id == user_id, "登录身份已变化", 401)
    action = fetch(db, AgentAction, action_id)
    require(action.user_id == user_id, "操作不存在", 404)
    require(not action.summary.get("content_redacted"), "此流程内容已清除，请重新准备", 409)
    version, dataset = visible_version(db, user, request.app.state.settings, action.version_id)
    if action.kind != "application":
        require(can_read_content(db, user, version, request.app.state.settings), "数据授权已变化", 403)
    if action.kind in {"vision", "dedup"}:
        require(manage_dataset(db, user, dataset), "数据负责人权限已变化", 403)
    row = fetch(db, AgentGraphWorkflow, action_id)
    require(row.user_id == user.id and row.action_id == action.id, "流程归属不一致", 404)
    require(row.graph_version == GRAPH_VERSION, "流程版本已变化，请使用对应版本恢复", 409)
    if token:
        require(row.lease_token == token and row.lease_until > now() and row.status not in INVALID,
                "流程执行租约已变化", 409)
    return user, action, row


def plan_hash(action):
    return digest([action.version_id, action.kind, action.payload, action.summary])


def build_graph(request, user_id, action_id, saver):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def validated(db, state):
        user, action, root = _authorize(db, request, user_id, action_id, token=saver.token)
        require(state["action_id"] == action.id and state["version_id"] == action.version_id and
                state["plan_hash"] == plan_hash(action), "执行方案已变化，请重新预览", 409)
        return user, action

    def plan(state):
        with saver.database.Session() as db:
            _, action = validated(db, state)
            require(action.status == "pending" and action.expires_at > now(), "确认单已过期或取消", 409)
        return {"stage": "awaiting_confirmation"}

    def approval(state):
        with saver.database.Session() as db:
            _, action = validated(db, state)
            require(action.status == "pending" and action.expires_at > now(), "确认单已过期或取消", 409)
        # This node is replayed after resume. No business side effect precedes interrupt.
        choice = interrupt({"kind": "confirmation_required", "action_id": action_id,
                            "plan_hash": state["plan_hash"]})
        require(type(choice) is dict and choice == {"approved": True}, "需要明确确认执行方案", 409)
        return {"approved": True, "stage": "confirmed"}

    def submit(state):
        require(state.get("approved") is True, "尚未确认执行方案", 409)
        from .assistant import execute_action
        with saver.database.Session() as db:
            user, action = validated(db, state)
            # Existing CAS + stable action id creates exactly one business receipt.
            # If the process dies after commit, replay returns that existing receipt.
            result = {"result": action.result} if action.status == 'completed' else \
                execute_action(action_id, request, db, user, dispatch_task=False)
        return {"stage": "submitted" if result["result"].get("job_id") else "completed"}

    def wait_task(state):
        from .agent_streams import state_snapshot
        with saver.database.Session() as db:
            validated(db, state)
            snapshot = state_snapshot(db, request, user_id, action_id)
        if snapshot["status"] == "completed":
            return {"stage": "completed", "task_status": "completed"}
        # Failed/cancelled jobs can be retried through the separately authorized jobs API.
        # Keep waiting here so that a later attempt can finish the same workflow.
        interrupt({"kind": "task_requires_attention" if snapshot['status'] in {'failed', 'cancelled'}
                   else "task_pending", "action_id": action_id})
        # A refresh resumes only this wait, and never creates or retries a task.
        with saver.database.Session() as db:
            validated(db, state)
            snapshot = state_snapshot(db, request, user_id, action_id)
        status = snapshot["status"]
        return {"stage": "completed" if status == "completed" else "awaiting_task",
                "task_status": status}

    builder = StateGraph(WorkflowState)
    for name, node in [("plan", plan), ("approval", approval), ("submit", submit), ("wait_task", wait_task)]:
        builder.add_node(name, node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "approval")
    builder.add_edge("approval", "submit")
    builder.add_conditional_edges("submit", lambda state: END if state["stage"] == "completed" else "wait_task")
    builder.add_conditional_edges("wait_task", lambda state: "wait_task" if state["stage"] == "awaiting_task" else END)
    return builder.compile(checkpointer=saver)


def dispatch_committed(request, user_id, action_id):
    from .tasks import dispatch
    with request.app.state.db.Session() as db:
        _, action, root = _authorize(db, request, user_id, action_id)
        if action.status != "completed" or not action.result.get("job_id"):
            return
        job_id, kind = action.result["job_id"], action.kind
    if kind == "vision":
        from .agent_workflows import start_vision
        start_vision(request.app, job_id)
    else:
        dispatch(request.app, job_id)


def drive(request, user_id, action_id, mode="recover"):
    """Only server-selected commands enter the graph; callers never send graph state."""
    from langgraph.types import Command
    from langsmith import tracing_context
    from .agent_graph_store import SQLGraphSaver, thread_config
    require(mode in {"start", "confirm", "recover", "refresh"}, "无效流程操作", 422)
    database, settings = request.app.state.db, request.app.state.settings
    with database.Session() as db:
        _, action, root = _authorize(db, request, user_id, action_id)
        require(root.status not in INVALID, "流程已取消或失效，请重新准备", 409)
        require(action.status != "pending" or action.expires_at > now(), "确认单已过期，请重新预览", 409)
        initial = {"action_id": action.id, "version_id": action.version_id,
                   "plan_hash": plan_hash(action), "approved": False, "stage": "created"}
    saver = SQLGraphSaver(database, action_id, lease_seconds=settings.agent_graph_lease_seconds)
    saver.acquire(uid())
    try:
        saver.prune_history()
        graph, config = build_graph(request, user_id, action_id, saver), thread_config(action_id)
        config.update({"recursion_limit": 12, "max_concurrency": 1})
        # Explicitly disable hosted tracing, even if the host has LangSmith env vars.
        with tracing_context(enabled=False):
            state = graph.get_state(config)
            if not state.values:
                graph.invoke(initial, config, durability="sync")
                state = graph.get_state(config)
            if mode == "confirm" and "approval" in state.next:
                graph.invoke(Command(resume={"approved": True}), config, durability="sync")
            elif mode in {"recover", "refresh", "confirm"} and state.next:
                if "wait_task" in state.next and state.interrupts:
                    graph.invoke(Command(resume={"refresh": True}), config, durability="sync")
                else:
                    # None never manufactures a confirmation; it only retries saved work.
                    graph.invoke(None, config, durability="sync")
            state = graph.get_state(config)
        stage = "awaiting_confirmation" if "approval" in state.next else \
            "awaiting_task" if "wait_task" in state.next and state.interrupts else state.values.get("stage", "interrupted")
        if stage == 'awaiting_task' and state.values.get('task_status') in {'failed', 'cancelled'}:
            stage = 'task_needs_attention'
        saver.prune_history()
        saver.release(stage)
    except BaseException as exc:
        code = f"validation_{exc.status_code}" if isinstance(exc, HTTPException) else "workflow_step_failed"
        saver.release("interrupted", code)
        raise
    # No graph session/lease spans S3/Redis/inline processing. Business dispatch is recoverable.
    if mode != "start":
        dispatch_committed(request, user_id, action_id)
    with database.Session() as db:
        user, action, _ = _authorize(db, request, user_id, action_id)
        result = public_action(db, action)
        audit(db, user, "agent.graph.advance", action.id, mode=mode, version_id=action.version_id,
              graph_version=GRAPH_VERSION, stage=result['orchestration']['status'])
        db.commit()
        return result


def invalidate(db, action, *, redacted=False):
    """Used in the same transaction as cancellation/conversation erasure."""
    row = db.get(AgentGraphWorkflow, action.id)
    if row:
        from .agent_graph_store import clear_checkpoints
        db.execute(update(AgentGraphWorkflow).where(AgentGraphWorkflow.id == row.id).values(
            status="redacted" if redacted else "cancelled", lease_token=None, lease_until=None))
        clear_checkpoints(db, row.id)


def workflow_status(request, user_id, action_id):
    with request.app.state.db.Session() as db:
        _, action, _ = _authorize(db, request, user_id, action_id)
        result = {"action_id": action.id, "orchestration": receipt(db, action)}
        if action.status == "completed" and action.result.get("job_id"):
            from .agent_streams import state_snapshot
            result["task"] = state_snapshot(db, request, user_id, action_id)
        return result
