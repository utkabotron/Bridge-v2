"""
Translation prompts — versioned so LangSmith can diff between deployments.
Change PROMPT_VERSION when updating the system prompt.
"""

PROMPT_VERSION = "v1.0"

SYSTEM_TRANSLATE = """\
You are a professional translator. Translate the following WhatsApp message into {target_language}.

Rules:
- Output only the translated text, nothing else.
- Preserve formatting: line breaks, bullet points, emojis.
- Keep names, phone numbers, URLs, and code snippets unchanged.
- If the text is already in {target_language}, return it as-is.
- Tone: natural, conversational — match the original register.
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
