"""Environment values more than one service parses."""
from __future__ import annotations

import os


def parse_ids(raw: str | None) -> list[int]:
    """"1, 2,,3" → [1, 2, 3].

    Blank entries (unset variable, trailing comma) are skipped; anything else that is not
    a number raises, so a typo stops the service at start instead of silently dropping an
    admin from alerts.
    """
    return [int(x) for x in (raw or "").split(",") if x.strip()]


def admin_tg_ids() -> list[int]:
    """Telegram user ids in ADMIN_TG_IDS: who gets alerts and digests, who is made an admin
    on /start, whose unpaired chats may fall back to the bot."""
    return parse_ids(os.getenv("ADMIN_TG_IDS"))
