#!/usr/bin/env python3
"""
Bridge v1 → v2 Migration Script
Reads from Supabase v1 via REST API, writes to Bridge v2 PostgreSQL.

WA sessions are NOT migrated — users must reconnect WhatsApp via /start.

Usage:
    pip install psycopg2-binary requests
    SUPABASE_URL=https://cvdnpmaklqbnbrquuydj.supabase.co \
    SUPABASE_SERVICE_KEY=eyJ... \
    NEW_DATABASE_URL=postgresql://bridge:bridge@localhost:5432/bridge \
    python infra/migrate_v1_to_v2.py
"""

import os
import sys
from datetime import timezone

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2-binary not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://cvdnpmaklqbnbrquuydj.supabase.co")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_SERVICE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImN2ZG5wbWFrbHFibmJycXV1eWRqIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1OTk0MTgyMywiZXhwIjoyMDc1NTE3ODIzfQ"
    ".YhTBYMwrWGSLyYBKAvzxwVaxLqS60KtNbpcDt-kbQ5U",
)
NEW_DATABASE_URL = os.environ.get(
    "NEW_DATABASE_URL", "postgresql://bridge:bridge@localhost:5432/bridge"
)


def supabase_get(path: str, params: dict | None = None) -> list:
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def migrate() -> None:
    print("Connecting to v2 PostgreSQL...")
    new_conn = psycopg2.connect(NEW_DATABASE_URL)
    new_conn.autocommit = False
    new_cur = new_conn.cursor()

    try:
        # ── Fetch active chat pairs from v1 ────────────────────────────────
        print("Fetching active chat pairs from Supabase v1...")
        pairs = supabase_get(
            "chat_pairs",
            params={
                "status": "eq.active",
                "select": "tg_user_id,wa_chat_id,wa_chat_name,tg_chat_id,tg_chat_title,created_at",
            },
        )

        if not pairs:
            print("No active chat pairs found in v1. Nothing to migrate.")
            return

        # ── Group pairs by tg_user_id ──────────────────────────────────────
        users_pairs: dict[int, list] = {}
        for pair in pairs:
            uid = pair["tg_user_id"]
            users_pairs.setdefault(uid, []).append(pair)

        # ── Fetch user details from v1 ─────────────────────────────────────
        tg_user_ids = list(users_pairs.keys())
        print(f"Found {len(tg_user_ids)} users with active chat pairs")

        users_info: dict[int, dict] = {}
        for uid in tg_user_ids:
            rows = supabase_get(
                "tg_users",
                params={"tg_user_id": f"eq.{uid}", "select": "tg_user_id,tg_username,created_at"},
            )
            if rows:
                users_info[uid] = rows[0]
            else:
                users_info[uid] = {"tg_user_id": uid, "tg_username": None, "created_at": None}

        # ── Migrate ────────────────────────────────────────────────────────
        total_pairs = 0

        for tg_user_id, user_pairs in users_pairs.items():
            info = users_info[tg_user_id]
            tg_username = info.get("tg_username")
            created_at = info.get("created_at")

            # Upsert into v2 users
            new_cur.execute(
                """
                insert into public.users (tg_user_id, tg_username, is_active, wa_connected, created_at)
                values (%s, %s, true, false, %s)
                on conflict (tg_user_id) do update
                    set tg_username = excluded.tg_username,
                        is_active   = true
                returning id
                """,
                (tg_user_id, tg_username, created_at),
            )
            v2_user_id = new_cur.fetchone()[0]

            # Upsert onboarding_sessions (state=done)
            new_cur.execute(
                """
                insert into public.onboarding_sessions (user_id, state, done_at)
                values (%s, 'done', now())
                on conflict (user_id) do update
                    set state   = 'done',
                        done_at = excluded.done_at
                """,
                (v2_user_id,),
            )

            # Upsert chat pairs
            for pair in user_pairs:
                new_cur.execute(
                    """
                    insert into public.chat_pairs
                        (user_id, wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title, status, created_at)
                    values (%s, %s, %s, %s, %s, 'active', %s)
                    on conflict (user_id, wa_chat_id, tg_chat_id) do update
                        set wa_chat_name  = excluded.wa_chat_name,
                            tg_chat_title = excluded.tg_chat_title,
                            status        = 'active'
                    """,
                    (
                        v2_user_id,
                        pair["wa_chat_id"],
                        pair.get("wa_chat_name"),
                        pair["tg_chat_id"],
                        pair.get("tg_chat_title"),
                        pair.get("created_at"),
                    ),
                )
                total_pairs += 1

            display_name = f"@{tg_username}" if tg_username else f"tg_id:{tg_user_id}"
            n = len(user_pairs)
            print(f"  ✓ {display_name}: {n} pair{'s' if n != 1 else ''}")

        new_conn.commit()

        print(f"\nDone: {len(tg_user_ids)} users, {total_pairs} chat pairs migrated")
        print("Note: users need to reconnect WhatsApp via /start")

    except Exception as exc:
        new_conn.rollback()
        print(f"\nERROR: migration failed — {exc}")
        raise
    finally:
        new_cur.close()
        new_conn.close()


if __name__ == "__main__":
    migrate()
