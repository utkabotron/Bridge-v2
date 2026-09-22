"""Bot-wide constants. Handlers import from here instead of hardcoding."""
from __future__ import annotations

# Languages the inline buttons under a direct translation offer.
# code → (name the processor's /translate understands, button label)
DIRECT_LANGUAGES: dict[str, tuple[str, str]] = {
    "he": ("Hebrew", "עברית"),
    "en": ("English", "English"),
}

# What a message typed into the bot is translated into before any button is pressed.
# Deliberately not the user's profile language: that one defaults to Russian, which is
# the language being typed — translating Russian into Russian answers nothing.
DIRECT_LANG_DEFAULT = "he"

# Processor language name → button code, so the delivered language can be ticked.
DIRECT_LANG_BY_NAME = {name: code for code, (name, _) in DIRECT_LANGUAGES.items()}
