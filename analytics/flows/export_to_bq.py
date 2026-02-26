"""Prefect flow: PostgreSQL → BigQuery export (hourly).

Pulls new message_events rows since last export and appends to BigQuery.
LangSmith already covers LLM observability; this flow gives data engineering
visibility: row counts, latencies, delivery rates — queryable with SQL in BQ.

Deploy:
  prefect deployment build flows/export_to_bq.py:export_to_bigquery \
    --name pg-to-bq-hourly --cron "0 * * * *" --apply
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import psycopg2
from google.cloud import bigquery
from prefect import flow, get_run_logger, task


DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
BQ_PROJECT = os.getenv("BIGQUERY_PROJECT", "")
BQ_DATASET = os.getenv("BIGQUERY_DATASET", "bridge_analytics")
BQ_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.message_events"


# ── Tasks ─────────────────────────────────────────────────

@task(retries=3, retry_delay_seconds=30, name="fetch-pg-rows")
def fetch_new_rows(since: datetime) -> list[dict]:
    """Fetch message_events rows created since last export."""
    logger = get_run_logger()

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        """
        select
          id, wa_message_id, chat_pair_id, sender_name,
          original_text, translated_text, message_type,
          media_s3_key, translation_ms, delivery_status,
          error_message, created_at
        from public.message_events
        where created_at > %s
        order by created_at
        limit 10000
        """,
        (since,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    logger.info("Fetched %d rows from PostgreSQL", len(rows))
    return rows


@task(retries=2, retry_delay_seconds=60, name="upload-to-bq")
def upload_to_bigquery(rows: list[dict]) -> int:
    """Append rows to BigQuery table, creating it if needed."""
    logger = get_run_logger()

    if not rows:
        logger.info("No rows to upload")
        return 0

    client = bigquery.Client(project=BQ_PROJECT)

    # Ensure dataset exists
    dataset_ref = bigquery.DatasetReference(BQ_PROJECT, BQ_DATASET)
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        client.create_dataset(bigquery.Dataset(dataset_ref))
        logger.info("Created BigQuery dataset %s", BQ_DATASET)

    # Schema
    schema = [
        bigquery.SchemaField("id", "INTEGER"),
        bigquery.SchemaField("wa_message_id", "STRING"),
        bigquery.SchemaField("chat_pair_id", "INTEGER"),
        bigquery.SchemaField("sender_name", "STRING"),
        bigquery.SchemaField("original_text", "STRING"),
        bigquery.SchemaField("translated_text", "STRING"),
        bigquery.SchemaField("message_type", "STRING"),
        bigquery.SchemaField("media_s3_key", "STRING"),
        bigquery.SchemaField("translation_ms", "INTEGER"),
        bigquery.SchemaField("delivery_status", "STRING"),
        bigquery.SchemaField("error_message", "STRING"),
        bigquery.SchemaField("created_at", "TIMESTAMP"),
    ]

    table_ref = bigquery.TableReference.from_string(BQ_TABLE)
    try:
        client.get_table(table_ref)
    except Exception:
        table = bigquery.Table(table_ref, schema=schema)
        client.create_table(table)
        logger.info("Created BigQuery table %s", BQ_TABLE)

    # Serialize datetimes
    for row in rows:
        if isinstance(row.get("created_at"), datetime):
            row["created_at"] = row["created_at"].isoformat()

    errors = client.insert_rows_json(table_ref, rows)
    if errors:
        logger.error("BigQuery insert errors: %s", errors)
        raise RuntimeError(f"BQ insert failed: {errors}")

    logger.info("Uploaded %d rows to BigQuery", len(rows))
    return len(rows)


@task(name="get-last-export-time")
def get_last_export_time() -> datetime:
    """Read high-water mark from BQ or fall back to 24h ago."""
    try:
        client = bigquery.Client(project=BQ_PROJECT)
        result = client.query(
            f"select max(created_at) as last_ts from `{BQ_TABLE}`"
        ).result()
        for row in result:
            if row.last_ts:
                return row.last_ts.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # First run: export last 24h
    from datetime import timedelta
    return datetime.now(timezone.utc) - timedelta(hours=24)


# ── Flow ──────────────────────────────────────────────────

@flow(name="export-to-bigquery", log_prints=True)
def export_to_bigquery():
    """Hourly PostgreSQL → BigQuery export for message_events."""
    since = get_last_export_time()
    rows = fetch_new_rows(since)
    count = upload_to_bigquery(rows)
    return {"exported_rows": count, "since": since.isoformat()}


if __name__ == "__main__":
    export_to_bigquery()
