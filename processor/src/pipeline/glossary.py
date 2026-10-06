"""Service-wide glossary: hand-curated renderings of names, shared by every chat.

Table glossary_global (migration 024) → Redis glossary_global:{lang} (GLOBAL_GLOSSARY_CACHE_TTL)
→ {source: {"translation", "note"}}. bridge_shared.chat_context.with_global_glossary merges
the entries a message mentions into its chat profile, overriding the chat's own guess.

Best-effort throughout: a Redis or Postgres hiccup yields {} and the message is translated
with the chat glossary alone, as before this existed.
"""
from __future__ import annotations

import json
import logging

from ..config import GLOBAL_GLOSSARY_CACHE_TTL

logger = logging.getLogger(__name__)


def _key(language: str) -> str:
    return f"glossary_global:{language}"


async def global_glossary(language: str) -> dict:
    """{source: {"translation", "note"}} for this target language; {} on any failure."""
    from .cache import get_redis

    try:
        raw = await get_redis().get(_key(language))
        if raw is not None:
            return json.loads(raw)
    except Exception:
        pass  # cache is best-effort

    try:
        from ..db import get_pool
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT source, translation, note FROM glossary_global WHERE target_language = $1",
            language,
        )
    except Exception as exc:
        logger.warning("Global glossary unavailable (%s) — chat glossaries only", exc)
        return {}

    entries = {
        r["source"]: {"translation": r["translation"], **({"note": r["note"]} if r["note"] else {})}
        for r in rows
    }
    try:
        await get_redis().setex(_key(language), GLOBAL_GLOSSARY_CACHE_TTL,
                                json.dumps(entries, ensure_ascii=False))
    except Exception:
        pass
    return entries


async def invalidate(language: str) -> None:
    from .cache import get_redis

    try:
        await get_redis().delete(_key(language))
    except Exception:
        pass
