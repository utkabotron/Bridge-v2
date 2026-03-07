import html as _html


def esc(text: str) -> str:
    """Escape for Telegram HTML."""
    return _html.escape(str(text))


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def escape_md(text: str) -> str:
    """Escape for Telegram Markdown v1 (for templates with DB data)."""
    for ch in ('\\', '*', '_', '`', '['):
        text = text.replace(ch, f'\\{ch}')
    return text
