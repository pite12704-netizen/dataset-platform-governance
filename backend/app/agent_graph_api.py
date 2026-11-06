"""Public commands are intentionally narrower than LangGraph's state/Command API."""
from typing import Literal
from fastapi import APIRouter, Depends, Request

from .agent_streams import stream_identity, identity
from .db import run_db
from .schemas import Input

router = APIRouter(prefix="/api/v1/agent", tags=["可恢复治理流程"])


class ResumeInput(Input):
    mode: Literal["recover", "refresh"] = "recover"


def inspect_workflow(db, request, user_id, action_id):
    from .agent_graph import workflow_status
    return workflow_status(request, user_id, action_id)


@router.get("/actions/{action_id}/workflow")
async def status(action_id: str, request: Request):
    user_id = await run_db(request, identity, request)
    return await run_db(request, inspect_workflow, request, user_id, action_id)


def recover_workflow(db, request, user_id, action_id, mode):
    from .agent_graph import drive
    return drive(request, user_id, action_id, mode)


@router.post("/actions/{action_id}/resume")
async def resume(action_id: str, data: ResumeInput, request: Request, user_id=Depends(stream_identity)):
    # Reuses bounded per-process and per-user admission; DB leases coordinate processes.
    return await run_db(request, recover_workflow, request, user_id, action_id, data.mode)
