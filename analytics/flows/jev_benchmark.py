"""One-off check: does Jev agree with the LLM judge on translations it already scored?

Re-scores past LLM evaluations from translation_evaluations with Jev and prints how close
the two are. Read-only — writes nothing to the database. Run it before turning on
JEV_MODE=primary, inside the analytics container:

  docker compose exec analytics python -m flows.jev_benchmark --limit 300

Only evaluations tied to a still-existing chat pair are used: orphaned events have no
reliable target language, which is what skewed the September quality numbers.
"""
from __future__ import annotations

import argparse
import json
import logging

from . import jev_eval
from .shared import db_conn


def load_llm_evaluations(limit: int) -> list[dict]:
    with db_conn() as conn:
        cur = conn.cursor()
        # One row per message: the same event can be sampled on more than one night.
        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (te.message_event_id)
                       te.message_event_id AS id, te.original_text, te.translated_text,
                       te.quality_score, te.issues_found, te.created_at,
                       COALESCE(cp.target_language, u.target_language) AS target_language
                FROM translation_evaluations te
                JOIN message_events me ON me.id = te.message_event_id
                JOIN chat_pairs cp ON cp.id = me.chat_pair_id
                JOIN users u ON u.id = cp.user_id
                WHERE te.evaluator = 'llm'
                  AND NOT te.shadow
                  AND te.quality_score IS NOT NULL
                  AND COALESCE(te.original_text, '') <> ''
                  AND COALESCE(te.translated_text, '') <> ''
                ORDER BY te.message_event_id, te.created_at DESC
            ) latest
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=300, help="most recent LLM evaluations to re-score")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("jev_benchmark")

    rows = load_llm_evaluations(args.limit)
    if not rows:
        log.info("No LLM evaluations to compare against.")
        return
    log.info("Re-scoring %d LLM-evaluated translations with Jev...", len(rows))

    llm_evals = [
        {
            "sample_key": jev_eval.sample_key(r),
            "quality_score": r["quality_score"],
            "issues": r["issues_found"] if isinstance(r["issues_found"], list) else json.loads(r["issues_found"] or "[]"),
        }
        for r in rows
    ]
    scored = jev_eval.evaluate_samples(rows, log=log)

    result = jev_eval.agreement(llm_evals, scored["evaluations"])
    result.update(jev_failed=scored["failed"], jev_tokens=scored["tokens_used"])
    print(json.dumps(result, indent=2))
    print(
        "\nRead it as: mae < 0.5 and within_1 > 0.9 means Jev tracks the LLM's scale; "
        "bad_recall is the share of translations the LLM called bad (<=3) that Jev caught too."
    )


if __name__ == "__main__":
    main()
