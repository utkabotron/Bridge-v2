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


# Telegram rejects a message over 4096 characters ("Message is too long"). An analysis of a
# long PDF came to 7736 on 10.10 and the reader got nothing.
TELEGRAM_LIMIT = 4096
# Raw text is cut below the limit: escaping can lengthen it (&lt; is four characters).
RAW_CHUNK = 3500


def split_text(text: str, limit: int = RAW_CHUNK) -> list[str]:
    """Raw text in pieces of at most `limit` characters, each cut at the last paragraph
    break before the limit, else the last line break, else the last space — a hard cut
    only inside a single word longer than the limit."""
    pieces, text = [], text or ""
    while len(text) > limit:
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, 0, limit + 1)
            if cut > 0:
                pieces.append(text[:cut])
                text = text[cut + len(sep):]
                break
        else:
            pieces.append(text[:limit])
            text = text[limit:]
    pieces.append(text)
    return pieces


def pack_messages(blocks: list[str], sep: str = "\n\n", limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Already-formatted HTML blocks (each well under the limit, tags closed) joined into as
    few messages as fit. A block is never cut, so no tag is split across messages."""
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}{sep}{block}" if current else block
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages
