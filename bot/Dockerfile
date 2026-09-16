FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements.lock ./
# Install the lock, not the ranges — see requirements.lock for why.
RUN pip install --no-cache-dir -r requirements.lock

COPY src/ ./src/

# Drop root: the service needs no privileged operation at runtime, and a bug in a
# dependency should not come with uid 0 inside the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

CMD ["python", "-m", "src.main"]
