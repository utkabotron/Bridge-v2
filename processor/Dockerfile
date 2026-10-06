FROM python:3.12-slim

WORKDIR /app

# Built from the repo root (docker-compose: context ., dockerfile processor/Dockerfile) so
# the shared package below is inside the build context.
COPY processor/requirements.txt processor/requirements.lock ./
# Install the lock, not the ranges — see requirements.lock for why.
RUN pip install --no-cache-dir -r requirements.lock

# Code shared with analytics and bot (prices, script regexes, chat context, esc). Next to
# src/, so it imports the way src does: /app is the working directory.
COPY shared/bridge_shared/ ./bridge_shared/
COPY processor/src/ ./src/

# Drop root: the service needs no privileged operation at runtime, and a bug in a
# dependency should not come with uid 0 inside the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
