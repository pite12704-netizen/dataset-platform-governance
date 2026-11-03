# Architecture and consistency

The product has catalog, production/governance, annotation/review and evaluation workspaces. They share identity, version metadata, storage and audit, rather than silently creating separate permission databases.

MySQL stores users, organizations, immutable version evidence, assets, uploads, grants, jobs and audit records. Private object storage holds file bytes. Redis/Celery dispatch work; durable job state and recovery live in SQL. The deployed service stack uses MySQL/S3/Celery; the optional sandbox deliberately substitutes isolated local dependencies.

Object storage and SQL do not share a transaction. Uploads therefore use reserved quota, finalizing/aborting states, content hashes and retryable cleanup. Files remain quarantined until checks complete. Published data is obtained through asset/version APIs, not arbitrary paths.

Review uses short SQL locks, a bounded claim lease and revision checks. Human editing does not hold a SQL transaction. Annotation reviewers confirm labels; organization administrators approve nonpublic publication, platform administrators approve public listing, and owners/submitters cannot self-approve.

Agent text cannot create arbitrary jobs. A persistent confirmation binds controlled parameters and input/template/model hashes. Confirmation rechecks authority and uses CAS plus a stable idempotency key. After commit, dispatch can be retried from durable state; the system does not claim exactly-once message delivery.

Vision parents advance bounded shards from persistent cursors. Large immutable manifests are separated from hot job fields. SSE reads small state snapshots and aggregates child progress without rewriting parent manifests; every read rechecks access and closes its SQL session before waiting. Reconnection restores the latest state, not a complete event log.

Model, metadata and dataset text are untrusted. No arbitrary SQL, filesystem, shell or Docker tools are exposed. Gate results and underlying version fingerprints matter; a fluent answer does not establish accuracy or successful execution.
