"""The service glossary of names, held in memory (docs/glossary-plan.md).

Tables `glossary` (status verified | locked) and `glossary_override` (one chat's own
rendering), migration 025. Both are small, so the whole of them lives in this process as
GlossaryIndex objects; a message costs a few hundred dict lookups and no I/O. At most once
per GLOSSARY_REFRESH_SECONDS a message triggers a cheap signature query (row counts and
max(updated_at)) and the indexes are rebuilt only when it changed — nobody has to
invalidate anything, and /api/glossary edits call reload() to apply at once.

Best-effort: if the database is unreachable the last loaded indexes stay in use, and
before the first successful load messages go out with chat glossaries alone.
"""
from __future__ import annotations

import asyncio
import logging
import time

from bridge_shared.glossary_match import GlossaryIndex

from ..config import GLOSSARY_REFRESH_SECONDS, GLOSSARY_USED_STATUSES

logger = logging.getLogger(__name__)

USED_STATUSES = GLOSSARY_USED_STATUSES

_lock = asyncio.Lock()
_signature: tuple | None = None
_checked_at = 0.0
_by_language: dict[str, GlossaryIndex] = {}
_overrides: dict[tuple[int, str], GlossaryIndex] = {}


# Many Israeli first names are everyday words too (עמוס "busy", קשת "rainbow", ישראל the
# country). People's entries carry no note of their own — relations stay in chat profiles —
# so they get this one, or "I'm busy" comes back as "I'm Amos".
PERSON_NOTE = "имя человека — только если слово здесь означает имя"


def _entry(row) -> dict:
    note = row["note"] or (PERSON_NOTE if row.get("kind") == "person" else None)
    return {"translation": row["translation"], **({"note": note} if note else {})}


async def _refresh() -> None:
    global _signature, _checked_at, _by_language, _overrides
    now = time.monotonic()
    if _signature is not None and now - _checked_at < GLOSSARY_REFRESH_SECONDS:
        return
    async with _lock:
        if _signature is not None and time.monotonic() - _checked_at < GLOSSARY_REFRESH_SECONDS:
            return  # another worker refreshed while we waited
        _checked_at = time.monotonic()
        try:
            from ..db import get_pool
            pool = await get_pool()
            sig = await pool.fetchrow("""
                SELECT (SELECT count(*) FROM glossary) AS g_count,
                       (SELECT max(updated_at) FROM glossary) AS g_max,
                       (SELECT count(*) FROM glossary_override) AS o_count,
                       (SELECT max(updated_at) FROM glossary_override) AS o_max
            """)
            signature = tuple(sig.values())
            if signature == _signature:
                return
            rows = await pool.fetch(
                "SELECT source, target_language, translation, note, kind FROM glossary "
                "WHERE status = ANY($1::text[]) AND translation IS NOT NULL",
                list(USED_STATUSES),
            )
            override_rows = await pool.fetch(
                "SELECT chat_pair_id, source, target_language, translation, note FROM glossary_override")
        except Exception as exc:
            logger.warning("Glossary reload failed (%s) — keeping %d loaded languages",
                           exc, len(_by_language))
            return

        by_language: dict[str, dict] = {}
        for r in rows:
            by_language.setdefault(r["target_language"], {})[r["source"]] = _entry(r)
        overrides: dict[tuple[int, str], dict] = {}
        for r in override_rows:
            overrides.setdefault((r["chat_pair_id"], r["target_language"]), {})[r["source"]] = _entry(r)

        _by_language = {lang: GlossaryIndex(e) for lang, e in by_language.items()}
        _overrides = {k: GlossaryIndex(e) for k, e in overrides.items()}
        _signature = signature
        logger.info("Glossary loaded: %s entries, %d chat overrides",
                    {lang: len(i) for lang, i in _by_language.items()}, len(override_rows))


async def lookup(language: str, text: str, chat_pair_id: int | None = None) -> dict[str, dict]:
    """{source: {"translation", "note"}} for every glossary name `text` mentions; the
    chat's override wins over the service entry for the same name."""
    await _refresh()
    hits: dict[str, dict] = {}
    index = _by_language.get(language)
    if index is not None:
        hits = index.find(text)
    override = _overrides.get((chat_pair_id, language)) if chat_pair_id else None
    if override is not None:
        own = override.find(text)
        if own:
            from bridge_shared.chat_context import covered_by_global
            hits = {k: v for k, v in hits.items() if not covered_by_global(k, own)}
            hits.update(own)
    return hits


def index_for(language: str) -> GlossaryIndex | None:
    """The loaded index, without a refresh — for callers that already did a lookup."""
    return _by_language.get(language)


def reload() -> None:
    """Make the next lookup re-read the tables (after an edit through the API)."""
    global _signature
    _signature = None
