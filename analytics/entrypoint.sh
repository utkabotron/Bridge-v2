#!/bin/sh
# Local dev: if PREFECT_API_URL is not set, start a local Prefect server.
# Production: PREFECT_API_URL points to Prefect Cloud → run the worker.

if [ -z "$PREFECT_API_URL" ]; then
  echo "PREFECT_API_URL not set — starting local Prefect server (dev mode)"
  exec prefect server start --host 0.0.0.0
else
  echo "Starting Prefect worker for pool: bridge-v2-pool"
  exec prefect worker start --pool "bridge-v2-pool" --type process
fi
