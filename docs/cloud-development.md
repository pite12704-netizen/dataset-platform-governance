# Cloud and other computers

Clone the reviewed public repository on the new computer and run the README's
isolated setup or `bash scripts/setup-cloud.sh`. Use `bash scripts/check-cloud.sh`
for tests with synthetic data. No maintenance script should target an existing
business database by default.

Cloud localhost is the cloud computer. Cloning source does not migrate Windows
MySQL/MinIO/Docker, LM Studio, user accounts, image data, encryption keys or the
maintainer's chat history. Obtain authorized data/services separately and follow
the [deployment guide](public/deployment.md) before changing deployment.

Never upload `.env`, database backups, model weights, signed URLs or chat exports
as a shortcut for connecting environments. Optional real-service tests should
allocate fresh isolated containers; ordinary CI should not receive production
credentials or connect to owner services.
