"""Prefect flow: nightly PostgreSQL backup.

There were no backups of any kind. The database holds every chat pair, user, chat profile
and glossary the bridge has built up, on a single VPS with a single local Docker volume —
losing the disk meant losing all of it and asking every user to re-onboard from scratch.

Deploy:
  registered by serve_flows.py with cron 30 2 * * *
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from prefect import flow, get_run_logger, task

from .shared import notify_telegram

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
KEEP = int(os.getenv("BACKUP_KEEP", "7"))
# A dump far smaller than the last one usually means it failed halfway.
MIN_BYTES = int(os.getenv("BACKUP_MIN_BYTES", "10240"))


@task(retries=1, retry_delay_seconds=30, name="dump-database")
def dump_database() -> Path:
    logger = get_run_logger()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"bridge-{stamp}.dump"

    # Custom format: compressed, and restorable table-by-table with pg_restore.
    result = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(target), DB_URL],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()[:500]}")

    size = target.stat().st_size
    if size < MIN_BYTES:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump produced only {size} bytes — treating as failed")

    logger.info("Backup written: %s (%.1f MB)", target.name, size / 1024 / 1024)
    return target


@task(name="verify-backup")
def verify_backup(path: Path) -> int:
    """Read the dump's table of contents — proves the file is a valid archive."""
    logger = get_run_logger()
    result = subprocess.run(
        ["pg_restore", "--list", str(path)], capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"backup is unreadable: {result.stderr.strip()[:300]}")

    entries = len([ln for ln in result.stdout.splitlines() if ln and not ln.startswith(";")])
    logger.info("Backup verified: %d objects", entries)
    return entries


@task(name="prune-backups")
def prune_backups() -> list[str]:
    logger = get_run_logger()
    dumps = sorted(BACKUP_DIR.glob("bridge-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in dumps[KEEP:]:
        removed.append(old.name)
        old.unlink(missing_ok=True)
    if removed:
        logger.info("Pruned %d old backup(s)", len(removed))
    return removed


@flow(name="nightly-backup", log_prints=True)
def nightly_backup():
    logger = get_run_logger()
    try:
        path = dump_database()
        objects = verify_backup(path)
        pruned = prune_backups()
    except Exception as exc:
        # A backup that fails quietly is the same as no backup at all.
        logger.error("Backup failed: %s", exc)
        notify_telegram(f"💾 <b>Database backup failed</b>\n\n<code>{str(exc)[:500]}</code>")
        raise

    return {"file": path.name, "objects": objects, "pruned": pruned}


if __name__ == "__main__":
    nightly_backup()
