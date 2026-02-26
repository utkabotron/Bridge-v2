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
