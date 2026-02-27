"""Prefect flow: nightly translation quality evaluation via LLM.

Samples recent translations, evaluates quality with LLM,
and generates suggestions for prompt improvement.

Deploy:
  prefect deployment build flows/translation_quality.py:translation_quality \
    --name translation-quality --cron "30 4 * * *" --apply
"""
from __future__ import annotations

import json
import os
from datetime import date

import httpx
import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_IDS = [int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]

EVAL_MODEL = "gpt-4o-mini"

# Current translation prompt (kept in sync manually with processor/src/pipeline/prompts.py)
CURRENT_PROMPT_VERSION = "v1.0"
CURRENT_PROMPT = """\
You are a professional translator. Translate the following WhatsApp message into {target_language}.

Rules:
- Output only the translated text, nothing else.
- Preserve formatting: line breaks, bullet points, emojis.
- Keep names, phone numbers, URLs, and code snippets unchanged.
- If the text is already in {target_language}, return it as-is.
- Tone: natural, conversational — match the original register.
"""

SAMPLE_SIZE = 50
BATCH_SIZE = 10


@task(retries=2, name="sample-translations")
def sample_translations() -> list[dict]:
    """Get stratified sample: 20 short + 20 long + 10 failed translations."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    samples = []

    # Short messages (< 100 chars original)
    cur.execute("""
        SELECT id, original_text, translated_text, translation_ms, delivery_status, error_message
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
          AND original_text IS NOT NULL
          AND translated_text IS NOT NULL
          AND length(original_text) < 100
          AND delivery_status = 'delivered'
        ORDER BY random()
        LIMIT 20
    """)
    samples.extend([dict(r) for r in cur.fetchall()])

    # Long messages (>= 100 chars original)
    cur.execute("""
        SELECT id, original_text, translated_text, translation_ms, delivery_status, error_message
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
          AND original_text IS NOT NULL
          AND translated_text IS NOT NULL
          AND length(original_text) >= 100
          AND delivery_status = 'delivered'
        ORDER BY random()
        LIMIT 20
    """)
    samples.extend([dict(r) for r in cur.fetchall()])

    # Failed translations
    cur.execute("""
        SELECT id, original_text, translated_text, translation_ms, delivery_status, error_message
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
          AND original_text IS NOT NULL
          AND delivery_status = 'failed'
        ORDER BY random()
        LIMIT 10
    """)
    samples.extend([dict(r) for r in cur.fetchall()])

    cur.close()
    conn.close()

    logger.info("Sampled %d translations (target: %d)", len(samples), SAMPLE_SIZE)
    return samples


@task(retries=1, name="evaluate-translations")
def evaluate_translations(samples: list[dict]) -> dict:
    """Evaluate translation quality in batches of 10."""
    logger = get_run_logger()

    # Filter to only samples that have both original and translated text
    evaluable = [s for s in samples if s.get("translated_text")]
    if not evaluable:
        logger.info("No translations to evaluate")
        return {"evaluations": [], "tokens_used": 0}

    client = OpenAI(api_key=OPENAI_API_KEY)
    all_evals = []
    total_tokens = 0

    system_prompt = """You are a translation quality evaluator for a WhatsApp→Telegram bridge.
Evaluate each translation pair and return a JSON array with one object per sample.

Each object must have:
- sample_index: int (0-based index matching input order)
- quality_score: 1-5 (overall quality)
- accuracy_score: 1-5 (meaning preserved correctly)
- naturalness_score: 1-5 (reads naturally in target language)
- issues: array of issue objects, each with:
  - type: one of "mistranslation", "lost_formatting", "wrong_tone", "unnecessary_addition", "omission", "grammar", "untranslated"
  - detail: brief description

Scoring guide:
- 5: Perfect or near-perfect
- 4: Good, minor issues only
- 3: Acceptable but noticeable problems
- 2: Poor, significant errors
- 1: Unusable or completely wrong

Return ONLY the JSON array, no markdown fences."""

    for i in range(0, len(evaluable), BATCH_SIZE):
        batch = evaluable[i : i + BATCH_SIZE]
        pairs = []
        for idx, s in enumerate(batch):
            pairs.append({
                "index": idx,
                "original": s["original_text"][:500],  # truncate for token efficiency
                "translated": s["translated_text"][:500],
            })

        response = client.chat.completions.create(
            model=EVAL_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(pairs, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=2000,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        batch_evals = json.loads(content)
        tokens = response.usage.total_tokens if response.usage else 0
        total_tokens += tokens

        # Attach message_event_id to each evaluation
        for ev in batch_evals:
            idx = ev.get("sample_index", 0)
            if idx < len(batch):
                ev["message_event_id"] = batch[idx]["id"]
                ev["original_text"] = batch[idx]["original_text"]
                ev["translated_text"] = batch[idx]["translated_text"]

        all_evals.extend(batch_evals)
        logger.info("Evaluated batch %d-%d (%d tokens)", i, i + len(batch), tokens)

    logger.info("Total evaluations: %d, tokens: %d", len(all_evals), total_tokens)
    return {"evaluations": all_evals, "tokens_used": total_tokens}


@task(retries=1, name="generate-suggestions")
def generate_suggestions(evaluations: list[dict]) -> dict:
    """Aggregate error patterns and suggest prompt improvements."""
    logger = get_run_logger()

    if not evaluations:
        logger.info("No evaluations, skipping suggestion generation")
        return {"suggestions": [], "tokens_used": 0}

    # Aggregate issue types
    issue_counts: dict[str, int] = {}
    issue_examples: dict[str, list[str]] = {}
    scores = {"quality": [], "accuracy": [], "naturalness": []}

    for ev in evaluations:
        scores["quality"].append(ev.get("quality_score", 0))
        scores["accuracy"].append(ev.get("accuracy_score", 0))
        scores["naturalness"].append(ev.get("naturalness_score", 0))
        for issue in ev.get("issues", []):
            itype = issue.get("type", "unknown")
            issue_counts[itype] = issue_counts.get(itype, 0) + 1
            if itype not in issue_examples:
                issue_examples[itype] = []
            if len(issue_examples[itype]) < 3:
                issue_examples[itype].append(issue.get("detail", ""))

    avg_scores = {k: round(sum(v) / len(v), 2) if v else 0 for k, v in scores.items()}

    client = OpenAI(api_key=OPENAI_API_KEY)

    system_prompt = """You are an expert in translation prompt engineering.
Given the current translation prompt and analysis of translation quality issues,
suggest specific improvements to the prompt.

Return a JSON array of suggestions. Each suggestion must have:
- suggestion: the specific change to make to the prompt (be concrete)
- rationale: why this change would help, referencing the error patterns

Focus on the most impactful changes. Return 1-5 suggestions max.
Return ONLY the JSON array, no markdown fences."""

    user_prompt = f"""Current prompt (version {CURRENT_PROMPT_VERSION}):
{CURRENT_PROMPT}

Average scores (1-5):
- Quality: {avg_scores['quality']}
- Accuracy: {avg_scores['accuracy']}
- Naturalness: {avg_scores['naturalness']}

Issue patterns found across {len(evaluations)} samples:
{json.dumps(issue_counts, indent=2)}

Examples of issues:
{json.dumps(issue_examples, indent=2, ensure_ascii=False)}"""

    response = client.chat.completions.create(
        model=EVAL_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=1500,
    )

    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    suggestions = json.loads(content)
    tokens = response.usage.total_tokens if response.usage else 0

    logger.info("Generated %d prompt suggestions (tokens: %d)", len(suggestions), tokens)
    return {
        "suggestions": suggestions,
        "tokens_used": tokens,
        "avg_scores": avg_scores,
        "issue_counts": issue_counts,
    }


@task(retries=2, name="store-quality-results")
def store_quality_results(eval_result: dict, suggestion_result: dict) -> int:
    """Store evaluations and suggestions in DB."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    total_tokens = eval_result.get("tokens_used", 0) + suggestion_result.get("tokens_used", 0)
    cost = total_tokens * 0.0003 / 1000

    summary = {
        "samples_evaluated": len(eval_result.get("evaluations", [])),
        "avg_scores": suggestion_result.get("avg_scores", {}),
        "issue_counts": suggestion_result.get("issue_counts", {}),
        "suggestions_count": len(suggestion_result.get("suggestions", [])),
    }

    cur.execute(
        """
        INSERT INTO nightly_analysis_runs (run_date, flow_type, summary, tokens_used, estimated_cost)
        VALUES (%s, 'translation_quality', %s, %s, %s)
        ON CONFLICT (run_date, flow_type) DO UPDATE
            SET summary = EXCLUDED.summary,
                tokens_used = EXCLUDED.tokens_used,
                estimated_cost = EXCLUDED.estimated_cost
        RETURNING id
        """,
        (date.today(), json.dumps(summary), total_tokens, cost),
    )
    run_id = cur.fetchone()[0]

    # Store individual evaluations
    for ev in eval_result.get("evaluations", []):
        cur.execute(
            """
            INSERT INTO translation_evaluations
                (run_id, message_event_id, original_text, translated_text,
                 quality_score, accuracy_score, naturalness_score, issues_found)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                ev.get("message_event_id"),
                ev.get("original_text", "")[:1000],
                ev.get("translated_text", "")[:1000],
                ev.get("quality_score"),
                ev.get("accuracy_score"),
                ev.get("naturalness_score"),
                json.dumps(ev.get("issues", [])),
            ),
        )

    # Store prompt suggestions
    for sug in suggestion_result.get("suggestions", []):
        cur.execute(
            """
            INSERT INTO prompt_suggestions (run_id, suggestion, rationale)
            VALUES (%s, %s, %s)
            """,
            (run_id, sug["suggestion"], sug.get("rationale", "")),
        )

    conn.commit()
    cur.close()
    conn.close()

    logger.info(
        "Stored run_id=%d: %d evaluations, %d suggestions",
        run_id,
        len(eval_result.get("evaluations", [])),
        len(suggestion_result.get("suggestions", [])),
    )
    return run_id


@task(retries=1, name="notify-quality-report")
def notify_quality_report(suggestion_result: dict) -> int:
    """Send translation quality report and prompt suggestions to admins."""
    logger = get_run_logger()

    avg_scores = suggestion_result.get("avg_scores", {})
    issue_counts = suggestion_result.get("issue_counts", {})
    suggestions = suggestion_result.get("suggestions", [])

    if not suggestions and not issue_counts:
        logger.info("No suggestions or issues, skipping notification")
        return 0

    if not TELEGRAM_BOT_TOKEN or not ADMIN_TG_IDS:
        logger.warning("Telegram not configured, cannot send quality report")
        return 0

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = ["📊 <b>Translation Quality Report</b>\n"]

    # Scores
    if avg_scores:
        lines.append("<b>Avg scores (1-5):</b>")
        lines.append(f"  Quality: {avg_scores.get('quality', '—')}")
        lines.append(f"  Accuracy: {avg_scores.get('accuracy', '—')}")
        lines.append(f"  Naturalness: {avg_scores.get('naturalness', '—')}\n")

    # Issues
    if issue_counts:
        lines.append("<b>Issues found:</b>")
        for itype, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {_esc(itype)}: {count}")
        lines.append("")

    # Suggestions
    if suggestions:
        lines.append(f"<b>Prompt suggestions ({len(suggestions)}):</b>\n")
        for i, sug in enumerate(suggestions, 1):
            lines.append(f"{i}. {_esc(sug['suggestion'])}")
            if sug.get("rationale"):
                lines.append(f"   <i>{_esc(sug['rationale'])}</i>\n")

    text = "\n".join(lines)
    sent = 0

    for chat_id in ADMIN_TG_IDS:
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            logger.error("Failed to notify admin %d: %s", chat_id, e)

    logger.info("Sent quality report to %d/%d admins", sent, len(ADMIN_TG_IDS))
    return sent


@flow(name="translation-quality", log_prints=True)
def translation_quality():
    """Nightly translation quality: sample → evaluate → suggest → store → notify."""
    samples = sample_translations()
    eval_result = evaluate_translations(samples)
    suggestion_result = generate_suggestions(eval_result.get("evaluations", []))
    run_id = store_quality_results(eval_result, suggestion_result)
    notified = notify_quality_report(suggestion_result)
    return {
        "run_id": run_id,
        "samples": len(samples),
        "evaluations": len(eval_result.get("evaluations", [])),
        "suggestions": len(suggestion_result.get("suggestions", [])),
        "admins_notified": notified,
    }


if __name__ == "__main__":
    translation_quality()
