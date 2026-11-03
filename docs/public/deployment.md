# Deployment modes

## Isolated learning

Use the README sandbox command in a fresh checkout. It allocates a new local SQLite database and object directory, generates a random application key and serves the built frontend on loopback. It creates no preset users. It disables real model, SMTP, OIDC and production scanners, so it must remain local.

`--prepare-only` builds the new namespace and migrations without starting an HTTP listener. Existing workspaces, account stores, Docker volumes and active-deployment pointers are not read or migrated. Restarting the default command creates another namespace; previous namespaces remain available for inspection rather than being deleted.

## Production-shaped stack

The component template is `deployment/compose.yml`, with settings documented in `deployment/service.env.example`. It requires explicit verified images for MySQL, the chosen S3/MinIO distribution and client, Redis and ClamAV; image/license/maintenance selection is an operator responsibility. Credentials belong in generated local secret files, not YAML or Git.

Provide MySQL application credentials and migrations, a private S3 bucket and application identity, Redis, ordinary/vision/mail workers, Beat and HTTPS ingress. API and workers must use matching images. Confirm pool budgets, object quotas, scanner limits, model concurrency and physical disk placement before enabling large input.

Do not run old seed, fresh-instance, recovery, integration or load scripts against an existing database. Back up and verify restoration before schema upgrades; stop/drain writers and explicitly review migration scope. An image build is not a database backup.

Keep storage keys stable and generate download links only after authorization. Configure trusted TLS and restrict infrastructure ports. Public registration needs real verification/SMTP; local spool files do not mean email was delivered. Private deployments can connect an intranet model, and catalog/review workflows remain useful without one.

Before offering a service externally, validate actual concurrent uploads/reviews, revoke access during streams, kill/recover a worker, verify backups on a fresh MySQL/S3 target and record throughput/resource limits. This repository does not contain the maintainer's deployment secrets, recovery disks, user accounts or hardware-specific runtime configuration.
