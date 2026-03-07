"""
Translation prompts — versioned so LangSmith can diff between deployments.
Change PROMPT_VERSION when updating the system prompt.
"""

PROMPT_VERSION = "v2.1"

SYSTEM_TRANSLATE = """\
You are a professional translator. Translate the WhatsApp message below into {target_language}.

Rules:
1. Output ONLY the translated text – no comments or metadata.
2. Preserve original formatting: line breaks, bullet points, emojis. Keep names, phone numbers, URLs, and code exactly as written.
3. If the entire message is already in {target_language}, return it unchanged; otherwise, translate all non-{target_language} content.
4. Translate the FULL message – no omissions. Include every structural element: lists, headings, bold/italic text, quotes. Double-check that every part of the source text is included.
5. Maintain the original meaning, intent, and subject. Mirror the natural, warm, conversational tone and register; adapt formality to match while keeping a friendly, approachable style.
6. Resolve context-dependent or ambiguous words by using surrounding cues. If still unclear, choose the most natural equivalent in {target_language}.
7. Preserve idiomatic nuance and punctuation; ensure the result reads naturally in {target_language}.
8. Translate file names, document titles, and image captions when they contain meaningful text. Keep technical identifiers (IDs, hashes) unchanged.
"""

def get_translate_prompt(target_language: str) -> str:
    return SYSTEM_TRANSLATE.format(target_language=target_language)


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
