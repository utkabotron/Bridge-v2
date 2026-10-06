"""The chat-context block appended to the translation system prompt.

Built from a chat_profiles row (analytics' chat-context builder writes it nightly). The
processor's translator sends it; the model bake-off must send exactly the same block or it
measures a prompt production does not use — it used to keep a "mirror" of this function.
"""
from __future__ import annotations


def format_chat_context(profile: dict | None) -> str:
    """Group description, tone, glossary and member names as a prompt block; "" when the
    profile has none of them."""
    if not profile:
        return ""
    parts = []

    if profile.get("chat_description"):
        parts.append(f"- Group: {profile['chat_description']}")
    if profile.get("tone"):
        parts.append(f"- Tone: {profile['tone']}")

    glossary = profile.get("glossary") or {}
    if glossary:
        items = []
        for word, info in glossary.items():
            if isinstance(info, dict):
                entry = f"{word} → {info.get('translation', '')}"
                if info.get("note"):
                    entry += f" ({info['note']})"
            else:
                entry = f"{word} → {info}"
            items.append(entry)
        # Named entities only. This used to say "use these transliterations", and the
        # glossary held everyday words, so the translator wrote "кадурсаль" for basketball.
        parts.append(
            "- Glossary — established renderings of names, places, organisations and "
            "programmes. Use them for these names only; translate everything else normally:\n  "
            + "\n  ".join(items)
        )

    members = profile.get("members") or {}
    if members:
        parts.append("- Member names:\n  " + "\n  ".join(f"{k} → {v}" for k, v in members.items()))

    if not parts:
        return ""
    return "\nChat context:\n" + "\n".join(parts)
