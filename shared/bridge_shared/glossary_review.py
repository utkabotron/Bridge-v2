"""The admin's review of proposed glossary names: one Telegram message, ✅ / ✏️ / ❌ per name.

Built here because two services send it: analytics after the morning digest, the bot when
the admin taps "next" or decides a name (the message is rebuilt in place). Everything a
button needs is inside the message — entry ids in callback data, the batch in the last
button — so the bot keeps nothing in memory and a tap works after a restart.

Callback data (≤ 64 bytes; ids in base 36):
  gl:ok:<id>   accept the resolver's rendering        → verified, decided_by admin
  gl:ed:<id>   ask for the admin's own rendering      → verified with it, decided_by admin
  gl:no:<id>   not a name / wrong reading             → rejected, decided_by admin
  gl:more:<ids>  next batch, skipping this one's ids (also carries the batch for rebuilds)
"""
from __future__ import annotations

from urllib.parse import urlparse

from .telegram_html import esc

BATCH = 10
REVIEW_COLUMNS = ("id, source, translation, kind, status, evidence, chat_renderings, "
                  "also_word, decided_by")

_MARK = {"verified": "✅", "rejected": "❌", "locked": "📌"}


def to36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def from36(s: str) -> int:
    return int(s, 36)


def batch_ids(callback_datas) -> list[int]:
    """Entry ids of a review message, from its keyboard's callback data."""
    for data in callback_datas:
        if data and data.startswith("gl:more:"):
            return [from36(x) for x in data[len("gl:more:"):].split(",") if x]
    return []


def _source_hint(evidence: str | None) -> str:
    """'Givolim · https://givolim.org.il/…' → 'Givolim · givolim.org.il'."""
    if not evidence:
        return ""
    parts = []
    for p in evidence.split(" · "):
        if p.startswith("http"):
            host = urlparse(p).netloc
            parts.append(host[4:] if host.startswith("www.") else host)
        else:
            parts.append(p)
    return " · ".join(x for x in parts if x)[:60]


def build(rows: list[dict], remaining: int) -> tuple[str, list[list[tuple[str, str]]]]:
    """(HTML text, keyboard rows of (label, callback_data)) for one batch.

    rows keep their order across rebuilds (the caller fetches them by the batch's ids);
    a decided one shows its mark and loses its buttons. `remaining` = proposed names not in
    this batch.
    """
    lines = ["🔤 <b>Имена на одобрение</b>",
             "✅ принять · ✏️ свой вариант · ❌ не имя · 🔸 пишется как обычное слово — "
             "работает только в чатах, где встречалось", ""]
    keyboard: list[list[tuple[str, str]]] = []
    for n, r in enumerate(rows, 1):
        renderings = r.get("chat_renderings") or {}
        chats = ", ".join(f"{esc(k)}×{v}" for k, v in sorted(renderings.items(), key=lambda kv: -kv[1]))
        translation = esc(r.get("translation") or "не имя?")
        decided = r.get("status") != "proposed"
        mark = (_MARK.get(r.get("status"), "") + " ") if decided else ""
        word = " 🔸" if r.get("also_word") else ""
        lines.append(f"{mark}{n}. <b>{esc(r['source'])}</b> → {translation}{word}")
        detail = []
        if chats:
            detail.append(f"в чатах: {chats}")
        hint = _source_hint(r.get("evidence"))
        if hint:
            detail.append(esc(hint))
        if detail:
            lines.append("    " + " · ".join(detail))
        if not decided:
            rid = to36(r["id"])
            keyboard.append([(f"{n} ✅", f"gl:ok:{rid}"), (f"{n} ✏️", f"gl:ed:{rid}"),
                             (f"{n} ❌", f"gl:no:{rid}")])
    ids = ",".join(to36(r["id"]) for r in rows)
    label = f"Следующие → (ещё {remaining})" if remaining else "Обновить"
    keyboard.append([(label, f"gl:more:{ids}")])
    if not remaining and all(r.get("status") != "proposed" for r in rows):
        lines += ["", "Всё разобрано 🎉"]
    return "\n".join(lines), keyboard


def edit_prompt(row: dict, batch_message_id: int, ids: list[int]) -> str:
    """The ForceReply question for ✏️; the last line lets the bot find its way back."""
    current = esc(row.get("translation") or "—")
    return (f"✏️ Как писать <b>{esc(row['source'])}</b>? Сейчас: {current}\n"
            "Ответьте на это сообщение правильным вариантом.\n"
            f"<code>ref g:{to36(row['id'])}:{batch_message_id}:{'.'.join(to36(i) for i in ids)}</code>")


def parse_ref(text: str) -> tuple[int, int, list[int]] | None:
    """(entry id, review message id, batch ids) from an edit prompt, or None."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("ref g:"):
            try:
                entry, message, ids = line[len("ref g:"):].split(":")
                return from36(entry), int(message), [from36(x) for x in ids.split(".") if x]
            except ValueError:
                return None
    return None
