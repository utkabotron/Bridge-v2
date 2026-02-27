#!/bin/sh
set -e

until mc alias set local http://minio:9000 bridge bridgeminio; do
  echo "Waiting for MinIO..."
  sleep 2
done

mc mb local/bridge-media --ignore-existing
mc anonymous set download local/bridge-media
echo "MinIO init done: bucket bridge-media created with public download policy"
