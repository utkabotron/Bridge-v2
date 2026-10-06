"""Escaping for Telegram's parse_mode=HTML."""
from __future__ import annotations

import html


def esc(text: object) -> str:
    """Escape &, <, > and quotes.

    Telegram's HTML mode accepts &quot; and numeric entities, so escaping quotes costs
    nothing in text and is what keeps a value safe inside an attribute (href). analytics
    had a copy without it. Anything is str()-ed first: a None or a number from the DB must
    not raise in the middle of building a message.
    """
    return html.escape(str(text))
