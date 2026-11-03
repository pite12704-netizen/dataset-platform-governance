#!/bin/sh
set -eu
mc alias set bootstrap http://minio:9000 "$(cat /run/secrets/minio_root_user)" "$(cat /run/secrets/minio_root_password)" >/dev/null
for bucket in datasets-integration datasets-recovery; do
    mc mb --ignore-existing bootstrap/$bucket >/dev/null
    mc anonymous set none bootstrap/$bucket >/dev/null
    mc version enable bootstrap/$bucket >/dev/null
done
mc admin user add bootstrap "$(cat /run/secrets/s3_test_access_key)" "$(cat /run/secrets/s3_test_secret_key)" >/dev/null
mc admin policy create bootstrap dataset-integration /bootstrap/integration-policy.json >/dev/null
mc admin policy attach bootstrap dataset-integration --user "$(cat /run/secrets/s3_test_access_key)" >/dev/null
echo 'Isolated integration and recovery buckets ready.'
