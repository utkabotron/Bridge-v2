"""Bot-wide constants. Handlers import from here instead of hardcoding."""
from __future__ import annotations

import re

# Languages the inline buttons under a direct translation offer.
# code → (name the processor's /translate understands, button label)
DIRECT_LANGUAGES: dict[str, tuple[str, str]] = {
    "ru": ("Russian", "Русский"),
    "he": ("Hebrew", "עברית"),
    "en": ("English", "English"),
}

# What a message typed into the bot is translated into before any button is pressed.
# Russian: the owner mostly pastes Hebrew in. A text that is already Russian goes to the
# fallback instead — translating Russian into Russian answers nothing.
DIRECT_LANG_DEFAULT = "ru"
DIRECT_LANG_FALLBACK = "he"

# Script of each language, to tell "already written in it" without a model call.
_SCRIPTS = {
    "ru": re.compile(r"[\u0400-\u04FF]"),
    "he": re.compile(r"[\u0590-\u05FF]"),
    "en": re.compile(r"[A-Za-z]"),
}


def default_direct_language(text: str) -> str:
    """Button code to translate a pasted text into before any button is pressed."""
    default_script = _SCRIPTS[DIRECT_LANG_DEFAULT]
    others = [rx for code, rx in _SCRIPTS.items() if code != DIRECT_LANG_DEFAULT]
    if default_script.search(text) and not any(rx.search(text) for rx in others):
        return DIRECT_LANG_FALLBACK
    return DIRECT_LANG_DEFAULT

# Processor language name → button code, so the delivered language can be ticked.
DIRECT_LANG_BY_NAME = {name: code for code, (name, _) in DIRECT_LANGUAGES.items()}
