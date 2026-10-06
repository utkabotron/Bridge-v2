FROM python:3.12-slim

WORKDIR /app

# Built from the repo root (docker-compose: context ., dockerfile bot/Dockerfile) so the
# shared package below is inside the build context.
COPY bot/requirements.txt bot/requirements.lock ./
# Install the lock, not the ranges — see requirements.lock for why.
RUN pip install --no-cache-dir -r requirements.lock

# Code shared with processor and analytics (script regexes, esc, ADMIN_TG_IDS). Next to
# src/, so it imports the way src does: /app is the working directory.
COPY shared/bridge_shared/ ./bridge_shared/
COPY bot/src/ ./src/

# Drop root: the service needs no privileged operation at runtime, and a bug in a
# dependency should not come with uid 0 inside the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

CMD ["python", "-m", "src.main"]
