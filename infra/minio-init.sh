#!/bin/sh
set -e

BUCKET="${S3_BUCKET:-bridge-media}"
MINIO_USER="${MINIO_ROOT_USER:?MINIO_ROOT_USER is required}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"

until mc alias set local http://minio:9000 "$MINIO_USER" "$MINIO_PASS"; do
  echo "Waiting for MinIO..."
  sleep 2
done

mc mb "local/${BUCKET}" --ignore-existing

# The bucket holds every photo, voice note and document that crosses the bridge, and the
# S3 API is reachable from the internet. It used to carry a public "download" policy, which
# let anyone list keys (they are prefixed by Telegram user id) and fetch any file. Readers
# authenticate with presigned URLs now — see processor/src/s3.py.
mc anonymous set none "local/${BUCKET}" || true

# Objects outlived their message_events rows forever: daily-cleanup drops the DB row after
# 90 days but never touched S3, so the bucket grew without bound toward filling the disk.
mc ilm rule add "local/${BUCKET}" --expire-days 90 2>/dev/null \
  || mc ilm add --expiry-days 90 "local/${BUCKET}" 2>/dev/null \
  || echo "WARN: could not set lifecycle rule (mc version?) — media will not auto-expire"

echo "MinIO init done: bucket ${BUCKET} is private, 90-day expiry set"
