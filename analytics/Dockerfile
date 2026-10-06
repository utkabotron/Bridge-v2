FROM python:3.12-slim

# pg_dump/pg_restore for the nightly backup flow.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Built from the repo root (docker-compose: context ., dockerfile analytics/Dockerfile) so
# the shared package below is inside the build context.
COPY analytics/requirements.txt analytics/requirements.lock ./
# Install the lock, not the ranges — see requirements.lock for why.
RUN pip install --no-cache-dir -r requirements.lock

# Code shared with processor and bot (prices, script regexes, chat context, esc). Next to
# flows/, so it imports the way flows does: /app is the working directory.
COPY shared/bridge_shared/ ./bridge_shared/
COPY analytics/flows/ ./flows/
COPY analytics/serve_flows.py ./serve_flows.py
COPY analytics/entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

# Drop root: the service needs no privileged operation at runtime, and a bug in a
# dependency should not come with uid 0 inside the container.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /backups \
    && chown -R appuser:appuser /app /backups
USER appuser

# Prefect writes its local state under $HOME.
ENV PREFECT_HOME=/home/appuser/.prefect \
    PREFECT_UI_STATIC_DIRECTORY=/home/appuser/.prefect/ui

EXPOSE 4200

CMD ["./entrypoint.sh"]
