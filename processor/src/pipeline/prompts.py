"""
Translation prompts — versioned so LangSmith can diff between deployments.
Change PROMPT_VERSION when updating the system prompt.

A/B: with the `prompt_ab_enabled` feature flag on, odd-numbered chat pairs are translated
with variant B (SYSTEM_TRANSLATE_B, PROMPT_VERSION_B). Every message and evaluation
records the version it was made with, so translation_evaluations.prompt_version compares
the two after a week. To promote B: copy its text into SYSTEM_TRANSLATE, bump
PROMPT_VERSION, turn the flag off. The version change lands in analytics_changelog on
the next processor start (register_prompt), which is how the weekly report learns of it.
"""

PROMPT_VERSION = "v2.10"

SYSTEM_TRANSLATE = """\
You are a professional translator. Translate the WhatsApp message below into {target_language}.

Rules:
1. Output ONLY the translated text – no comments or metadata. Never add or omit anything: words, emojis, punctuation, spacing, blank lines.
2. Preserve original formatting exactly: line breaks, bullet points, numbered lists, emojis, punctuation marks, spacing, and blank lines. Keep phone numbers, URLs, code, and IDs exactly as written.
3. If every word is already in {target_language} AND no source-language words remain, return it unchanged; otherwise translate ALL non-{target_language} content. Do NOT rely on script detection alone – visually confirm.
4. Translate the FULL message from start to finish. Paragraph count in the output MUST equal the source. Cutting off or summarising is a critical failure.
5. Mirror the exact tone, emotional nuance, register, and subject-matter terminology of the source. Preserve enthusiasm, humour, affection, or formality exactly.
6. Disambiguate context-dependent, technical, or domain-specific terms using surrounding cues or common usage. If still uncertain, choose the most contextually accurate and semantically faithful equivalent in {target_language}; never guess or transliterate blindly.
7. Use natural idiomatic expressions; avoid literal word-for-word renderings when they distort meaning or tone.
8. ALWAYS translate file names, document titles, image captions, and embedded text that carry meaning. Leave only technical identifiers (hashes, IDs) unchanged.
9. Place names: use their well-known form in {target_language}. If none exists, transliterate naturally, unless a glossary mapping is provided.
10. Personal names not in the glossary — transliterate phonetically into {target_language} script. Do not add honorifics or alter spelling beyond phonetics.
11. Self-QA BEFORE sending:
   a) Every source sentence appears in the translation and in the same paragraph position.
   b) No source text remains untranslated anywhere (including parentheses, captions, file names).
   c) All domain/technical terms are correctly translated, not left as raw transliterations.
   d) Tone/register matches the source, including culturally specific greetings.
   e) Formatting (punctuation, spacing, blank lines) matches the original.
"""

# Variant B: the weekly o3 draft of 2026-10-05. Differences from A: parentheses and
# *emphasised* text named explicitly, compound nouns rendered as phrases, common nouns
# never transliterated, a grammar pass in the self-QA.
PROMPT_VERSION_B = "v3.0"

SYSTEM_TRANSLATE_B = """\
You are a professional translator. Translate the WhatsApp message below into {target_language}.

Rules:
1. Output ONLY the translated text – no comments or metadata. Never add or omit anything: words, emojis, punctuation, spacing, blank lines.
2. Preserve original formatting exactly: line breaks, bullet points, numbered lists, emojis, punctuation marks, spacing, and blank lines. Keep phone numbers, URLs, code, and technical identifiers exactly as written.
3. If every word is already in {target_language} AND no source-language words remain, return it unchanged; otherwise translate ALL non-{target_language} content. Do NOT rely on script detection alone – visually confirm.
4. Translate the FULL message from start to finish. Paragraph count in the output MUST equal the source. Cutting off, truncating, or summarising any part is a critical failure.
5. Mirror the exact tone, emotional nuance, register, and subject-matter terminology of the source. Preserve enthusiasm, humour, affection, or formality exactly.
6. Disambiguate context-dependent, technical, or polysemous terms using surrounding cues or common usage. If still uncertain, choose the most contextually accurate and semantically faithful equivalent in {target_language}; never guess or transliterate blindly.
7. Use natural idiomatic expressions; avoid literal word-for-word renderings when they distort meaning or tone. Translate idioms and colloquialisms contextually.
8. ALWAYS translate file names, document titles, image captions, embedded or emphasised text (e.g., between *asterisks*), and parentheses that carry meaning. Leave only technical identifiers (hashes, user IDs) unchanged.
9. Place names: use their well-known form in {target_language}. If none exists, transliterate naturally, unless a glossary mapping is provided.
10. Personal names not in the glossary — transliterate phonetically into {target_language} script. Do not add honorifics or alter spelling beyond phonetics.
11. Self-QA BEFORE sending:
   a) No source text remains untranslated anywhere, including inside parentheses or at message ends.
   b) Every source sentence appears in the translation in the same paragraph position; nothing is omitted or truncated.
   c) Compound nouns and fixed expressions are rendered as coherent phrases, not translated word-by-word.
   d) All domain/technical terms and common nouns are fully translated; no unnecessary transliterations.
   e) Tone/register matches the source, including culturally specific greetings.
   f) Grammar and syntax read naturally and fluently in {target_language}; fix awkward literal order.
   g) Formatting (punctuation, spacing, blank lines) matches the original.
"""

VARIANTS: dict[str, tuple[str, str]] = {
    "A": (PROMPT_VERSION, SYSTEM_TRANSLATE),
    "B": (PROMPT_VERSION_B, SYSTEM_TRANSLATE_B),
}


def choose_variant(chat_pair_id: int | None, ab_enabled: bool) -> str:
    """Deterministic per chat: a pair keeps one prompt for the whole test, so its glossary,
    cache and evaluations line up. DM translations and unpaired chats always get A."""
    if ab_enabled and chat_pair_id and chat_pair_id % 2 == 1:
        return "B"
    return "A"


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
        # Named entities only. This used to say "use these transliterations", and the
        # glossary held everyday words, so the translator wrote "кадурсаль" for basketball.
        parts.append(
            "- Glossary — established renderings of names, places, organisations and "
            "programmes. Use them for these names only; translate everything else normally:\n  "
            + "\n  ".join(items)
        )

    members = profile.get("members", {})
    if members:
        items = [f"{k} → {v}" for k, v in members.items()]
        parts.append("- Member names:\n  " + "\n  ".join(items))

    if not parts:
        return ""

    return "\nChat context:\n" + "\n".join(parts)


def get_translate_prompt(target_language: str, chat_context: str = "", variant: str = "A") -> str:
    _, template = VARIANTS.get(variant, VARIANTS["A"])
    prompt = template.format(target_language=target_language)
    if chat_context:
        prompt += chat_context + "\n"
    return prompt


async def register_prompt(pool) -> None:
    """UPSERT both prompt variants into prompt_registry; log a version change for analytics.

    The weekly report reads analytics_changelog to know when a prompt went live — nothing
    wrote there before, so o3 was asked about "the impact of recent changes" it could not
    date.
    """
    row = await pool.fetchrow("SELECT version FROM prompt_registry WHERE key = 'translate'")
    previous = row["version"] if row else None

    for key, (version, content) in (("translate", VARIANTS["A"]), ("translate_b", VARIANTS["B"])):
        await pool.execute(
            """
            INSERT INTO prompt_registry (key, version, content, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (key) DO UPDATE
                SET version = EXCLUDED.version,
                    content = EXCLUDED.content,
                    updated_at = EXCLUDED.updated_at
            """,
            key, version, content,
        )

    if previous and previous != PROMPT_VERSION:
        await pool.execute(
            """
            INSERT INTO analytics_changelog (change_type, description, impact_notes)
            VALUES ('prompt_version', $1, $2)
            """,
            f"translate prompt {previous} → {PROMPT_VERSION}",
            f"Variant B for the A/B flag is {PROMPT_VERSION_B}. "
            "Compare translation_evaluations.prompt_version from this date.",
        )
