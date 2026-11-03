# Code navigation

Start with the public [architecture guide](docs/public/architecture.md).

- `backend/app/main.py` creates the API and resource lifecycles.
- `models.py`, `schemas.py` and `policy.py` define metadata, input contracts and access policy.
- `uploads.py` and `imports.py` implement resumable input and controlled batch ingestion.
- `tasks.py`, `dispatching.py`, `vision_plans.py` and `job_payloads.py` manage durable jobs, recovery and hot/cold state.
- `review_templates.py`, `image_completeness.py`, `joint_review.py` and `review_workbench.py` implement the human review workflow.
- `quality_contract.py`, `delivery.py`, `export_formats.py` and `downloads.py` bind evidence to delivery and access.
- `assistant.py`, `agent_tools.py`, `agent_runtime.py`, `agent_workflows.py` and `agent_streams.py` implement controlled Agent requests, confirmation and progress.
- `frontend/src/pages` contains workspaces; `api.ts`, `uploads.ts`, `agent-progress.ts` contain shared client behavior.
- `backend/tests` and `frontend/tests` contain synthetic permission, failure and concurrency cases.

Trace one request across input validation, actor/version policy, transaction, durable job, worker and final artifact. For confirmation, inspect `prepare_action → execute_action → enqueue_vision → advance → state_snapshot`.

The public snapshot omits maintainer chat, machine recovery notes, private deployment files, career drafts and the historical nested user-management repository. Their absence does not remove current API code.
