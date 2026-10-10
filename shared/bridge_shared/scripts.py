"""Which writing system a text is in — answered without a model call.

The processor skips translating a message already written in the target's script and
catches a model that handed the source back; the bot picks the default language of a DM
translation; the bake-off counts Hebrew left in a candidate. Each kept its own copy of
these ranges, two of them spelled as literal characters nobody could read.
"""
from __future__ import annotations

import re

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
# Scripts the bridge translates FROM: Hebrew, Arabic, Syriac.
SOURCE_SCRIPT_RE = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0700-\u074F]")
CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
LATIN_RE = re.compile(r"[A-Za-z]")

# Target language as stored on users/chat_pairs (any case) → the script it is written in.
TARGET_SCRIPT_RE: dict[str, re.Pattern[str]] = {
    "russian": CYRILLIC_RE,
    # The bot's voice notes are translated into Hebrew (processor media_analyzer.direct_voice);
    # without this entry its "is the answer really Hebrew" check silently passed everything.
    "hebrew": HEBREW_RE,
    "ukrainian": CYRILLIC_RE,
    "english": LATIN_RE,
    "spanish": LATIN_RE,
    "french": LATIN_RE,
    "german": LATIN_RE,
    "portuguese": LATIN_RE,
}


def target_script_re(target_language: str | None) -> re.Pattern[str] | None:
    """The script regex of a target language, None when we cannot tell — callers then
    assume nothing rather than guess."""
    return TARGET_SCRIPT_RE.get((target_language or "").strip().lower())
