# esc is shared with the bot and analytics (bridge_shared.telegram_html); callers import it
# from here as before.
from bridge_shared.telegram_html import esc


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"
