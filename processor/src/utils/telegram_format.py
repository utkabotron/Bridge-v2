import html as _html


def esc(text: str) -> str:
    """Escape for Telegram HTML."""
    return _html.escape(str(text))


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"
