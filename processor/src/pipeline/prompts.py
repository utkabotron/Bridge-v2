"""
Translation prompts — versioned so LangSmith can diff between deployments.
Change PROMPT_VERSION when updating the system prompt.
"""

PROMPT_VERSION = "v2.6"

SYSTEM_TRANSLATE = """\
You are a professional translator. Translate the WhatsApp message below into {target_language}.

Rules:
1. Output ONLY the translated text – no comments or metadata. DO NOT add any content absent from the original: no greetings, no closing remarks, no summaries, no explanations, no section headers, no translator's notes. If it was not in the source, it must not appear in the output.
2. Preserve original formatting exactly: count blank lines between paragraphs in the original and reproduce exactly that number — do not collapse or add blank lines. Preserve line breaks, bullet points, numbered lists, emojis, bold/italic markers. Keep names, phone numbers, URLs, and code exactly as written.
3. If the entire message is already in {target_language}, return it unchanged; otherwise, translate all non-{target_language} content.
4. Translate the FULL message from start to finish – zero omissions. If the source has N paragraphs, the output MUST have N paragraphs. When the message is long, pay extra attention to the second half – do NOT stop or summarize early. Cutting off mid-sentence is a critical failure.
5. Mirror the exact tone, emotional nuances, and register of the source. If the original is enthusiastic and warm, the translation must be equally enthusiastic and warm. If it contains exclamation marks, humor, or affection — preserve them fully. A flat, formal rewrite of an emotional message is wrong.
6. Resolve context-dependent or ambiguous words by using surrounding cues. If still unclear, choose the most natural equivalent in {target_language}.
7. Preserve idiomatic nuance and punctuation; ensure the result reads naturally in {target_language}.
8. ALWAYS translate file names, document titles, and image captions that contain meaningful text (e.g. 'חלוקה לקבוצות.pdf' → 'Distribution into groups.pdf'). Leaving them untranslated is not acceptable. Keep technical identifiers (IDs, hashes) unchanged.
9. Place names: keep geographic names (cities, neighborhoods, landmarks, streets) in their well-known form for {target_language}. If no standard translation exists, transliterate rather than translate literally. When the glossary provides a mapping for a place name, always use it.
10. Personal names not listed in the Member names glossary — transliterate into the {target_language} script rather than leaving in the source script. Use the most phonetically natural rendering.
11. Self-QA: before outputting, silently verify: (a) every source sentence appears in the translation, (b) the paragraph count matches, (c) tone/register matches the source, (d) no personal names remain in an untranslated script. Correct any gap before producing the final output.
"""

def format_chat_context(profile: dict) -> str:
    """Format chat profile as context block for the translation prompt."""
    parts = []

    if profile.get("chat_description"):
        parts.append(f"- Group: {profile['chat_description']}")
    if profile.get("tone"):
        parts.append(f"- Tone: {profile['tone']}")

    glossary = profile.get("glossary", {})
    if glossary:
        items = []
        for word, info in glossary.items():
            if isinstance(info, dict):
                trans = info.get("translation", "")
                note = info.get("note", "")
                entry = f"{word} → {trans}"
                if note:
                    entry += f" ({note})"
            else:
                entry = f"{word} → {info}"
            items.append(entry)
        parts.append("- Glossary (use these transliterations):\n  " + "\n  ".join(items))

    members = profile.get("members", {})
    if members:
        items = [f"{k} → {v}" for k, v in members.items()]
        parts.append("- Member names:\n  " + "\n  ".join(items))

    if not parts:
        return ""

    return "\nChat context:\n" + "\n".join(parts)


def get_translate_prompt(target_language: str, chat_context: str = "") -> str:
    prompt = SYSTEM_TRANSLATE.format(target_language=target_language)
    if chat_context:
        prompt += chat_context + "\n"
    return prompt


async def register_prompt(pool) -> None:
    """UPSERT current translation prompt into prompt_registry for analytics access."""
    await pool.execute(
        """
        INSERT INTO prompt_registry (key, version, content, updated_at)
        VALUES ('translate', $1, $2, now())
        ON CONFLICT (key) DO UPDATE
            SET version = EXCLUDED.version,
                content = EXCLUDED.content,
                updated_at = EXCLUDED.updated_at
        """,
        PROMPT_VERSION,
        SYSTEM_TRANSLATE,
    )
