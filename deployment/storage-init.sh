#!/bin/sh
set -eu
until mc alias set bootstrap http://minio:9000 "$(cat /run/secrets/minio_root_user)" "$(cat /run/secrets/minio_root_password)" >/dev/null 2>&1; do sleep 2; done
mc mb --ignore-existing bootstrap/datasets >/dev/null
mc anonymous set none bootstrap/datasets >/dev/null
mc version enable bootstrap/datasets >/dev/null
mc admin user add bootstrap "$(cat /run/secrets/s3_access_key)" "$(cat /run/secrets/s3_secret_key)" >/dev/null
mc admin policy create bootstrap dataset-app /bootstrap/bucket-policy.json >/dev/null
mc admin policy attach bootstrap dataset-app --user "$(cat /run/secrets/s3_access_key)" >/dev/null
echo 'Private versioned bucket and dedicated application user ready.'
