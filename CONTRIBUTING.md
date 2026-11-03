# Contributing

Use Python 3.12 and Node.js 22. Read `AGENTS.md` and the public quickstart first.
Work on a branch and keep changes scoped to one behavior. Describe the problem,
resulting behavior, relevant tests and any migration or compatibility impact.

Run `bash scripts/setup-cloud.sh` then `bash scripts/check-cloud.sh` on a clean
checkout, or use the documented equivalent commands on Windows. The ordinary
checks use isolated SQLite/local storage and controlled model responses. Docker
integration scripts must allocate their own namespaces and ports. Never aim tests,
seeds, migrations, cleanup or load generators at an existing business instance.

Before submitting, run `python scripts/audit_repository.py` and
`python scripts/audit_git_history.py`. Before publication also audit the index with
`python scripts/audit_repository.py --staged`. Do not attach `.env`, account exports,
customer data, real image samples, model weights, signed URLs or raw service logs.
Use synthetic fixtures and redact error messages, request headers and screenshots.

Agent changes must preserve version authorization, explicit confirmation for side
effects, stable idempotency and checks before execution and final output. Model
text and dataset metadata are untrusted inputs. Do not add arbitrary SQL, shell,
filesystem or Docker tools. A model response is not proof a job succeeded.

Add a migration for schema changes and document upgrade/backup expectations.
Do not stage the historical nested repository as a gitlink. Public examples must
use relative paths and placeholder domains, without a maintainer's machine paths.

By submitting a contribution, you confirm you have the right to contribute it
under this repository's MIT license; preserve any required third-party notices.
