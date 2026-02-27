#!/bin/sh
# Local mode: start Prefect server in background, then serve all flows on schedule.
# Cloud mode: PREFECT_API_URL set → run worker against Prefect Cloud.

if [ -z "$PREFECT_API_URL" ]; then
  echo "Starting local Prefect server..."
  prefect server start --host 0.0.0.0 &
  # Wait for server to be ready
  export PREFECT_API_URL="http://127.0.0.1:4200/api"
  echo "Waiting for Prefect server to be ready..."
  until python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4200/api/health')" 2>/dev/null; do
    sleep 2
  done
  sleep 3
  echo "Registering and serving flows on schedule..."
  exec python serve_flows.py
else
  echo "Starting Prefect worker for pool: bridge-v2-pool"
  exec prefect worker start --pool "bridge-v2-pool" --type process
fi
