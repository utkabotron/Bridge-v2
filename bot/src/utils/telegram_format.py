# esc is shared with the processor and analytics (bridge_shared.telegram_html); callers
# import it from here as before.
from bridge_shared.telegram_html import esc


def italic(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def escape_md(text: str) -> str:
    """Escape for Telegram Markdown v1 (for templates with DB data)."""
    for ch in ('\\', '*', '_', '`', '['):
        text = text.replace(ch, f'\\{ch}')
    return text
