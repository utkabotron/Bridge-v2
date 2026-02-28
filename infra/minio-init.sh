#!/bin/sh
set -e

BUCKET="${S3_BUCKET:-bridge-media}"

until mc alias set local http://minio:9000 bridge bridgeminio; do
  echo "Waiting for MinIO..."
  sleep 2
done

mc mb "local/${BUCKET}" --ignore-existing
mc anonymous set download "local/${BUCKET}"
echo "MinIO init done: bucket ${BUCKET} created with public download policy"
