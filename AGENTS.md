# Project guidance

This is an enterprise image dataset governance platform. Read README.md,
CODE_STUDY_GUIDE.md, and docs/cloud-development.md before changing deployment.

- Current application: backend/app (FastAPI), frontend/src (React/TypeScript).
- usermanage is historical learning source with its own local Git repository;
  keep its Git metadata out of the parent repository. It is not the current API.
- Production MySQL, private MinIO, Redis, Celery, Docker, and LM Studio are on
  the owner's Windows computer. Cloud localhost is a different machine.
- Cloud setup: bash scripts/setup-cloud.sh; isolated checks: bash scripts/check-cloud.sh.
- Do not run database migration, seed, cleanup, integration, restore, or load-test
  scripts against existing business services without explicit task authorization.
- Access business files through authorized dataset/asset APIs. Do not add
  unrestricted SQL, filesystem, shell, Docker socket, or model tools to the Agent.
- Credentials, account exports, datasets, model weights, .env files, and local
  deployment files must stay out of Git. Run scripts/audit_repository.py before
  staging and again with --staged before publishing. Never print credential values.
- A private Git repository and a cloud task do not migrate this local chat's history.
  docs/cloud-handoff.md supplies a concise, non-secret project handoff.
