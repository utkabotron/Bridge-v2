FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flows/ ./flows/
COPY serve_flows.py ./serve_flows.py
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

# Drop root: the service needs no privileged operation at runtime, and a bug in a
# dependency should not come with uid 0 inside the container.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Prefect writes its local state under $HOME.
ENV PREFECT_HOME=/home/appuser/.prefect

EXPOSE 4200

CMD ["./entrypoint.sh"]
