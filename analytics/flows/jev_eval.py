"""Translation quality scoring with TypeSafe's Jev classifier.

The LLM judge in translation_quality reads ~50 translations a night and writes a 1-5 score
and an issue list for each. Jev answers the same questions as typed probabilities, fast
and cheap enough to score every translation instead of a sample — but it writes no text,
so the LLM still describes the worst few (see translation_quality.JEV_MODE).

Kept free of Prefect and the database so the nightly flow and jev_benchmark share it and
it can be tested on its own.
"""
from __future__ import annotations

import os
import re

TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "")
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")
JEV_TIMEOUT = float(os.getenv("JEV_TIMEOUT", 15))
# An issue counts as found when Jev is at least this sure of it.
JEV_ISSUE_THRESHOLD = float(os.getenv("JEV_ISSUE_THRESHOLD", 0.5))
# Jev's quality at or below this marks a translation as bad (LLM equivalent: score <= 3).
JEV_BAD_QUALITY = float(os.getenv("JEV_BAD_QUALITY", 3.5))
# Consecutive failed requests that mean Jev is down rather than one sample being odd. Each
# failure already spent the SDK's retries; without a cutoff an outage costs an hour.
JEV_MAX_CONSECUTIVE_FAILURES = int(os.getenv("JEV_MAX_CONSECUTIVE_FAILURES", 5))
STATE_TEXT_LIMIT = 2000

# Same rubric the LLM judge is given, so the two scales mean the same thing.
QUALITY_LEVELS = [
    "Unusable or completely wrong.",
    "Poor, significant errors.",
    "Acceptable but noticeable problems.",
    "Good, minor issues only.",
    "Perfect or near-perfect.",
]
ACCURACY_LEVELS = [
    "Meaning lost or reversed.",
    "Major meaning errors.",
    "Some meaning lost or distorted.",
    "Meaning preserved with minor slips.",
    "Meaning fully preserved.",
]
NATURALNESS_LEVELS = [
    "Unreadable.",
    "Awkward throughout.",
    "Understandable, but clearly a translation.",
    "Mostly natural.",
    "Reads as if written by a native speaker.",
]

# The LLM judge's issue types, phrased as statements for Jev to confirm or reject.
ISSUE_STATEMENTS = {
    "mistranslation": "Part of the translation says something different from the original.",
    "omission": "The translation leaves out content that is in the original.",
    "unnecessary_addition": "The translation adds content that is not in the original.",
    "wrong_tone": "The translation uses a noticeably different tone or register than the original.",
    "grammar": "The translation contains grammar errors in the target language.",
    "lost_formatting": "The translation loses formatting of the original: line breaks, lists, emphasis or emoji.",
    "untranslated": "The translation is not in the target language, or leaves parts of the original untranslated.",
}

_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask_pii(text: str) -> str:
    """Replace phone numbers and emails before the text leaves for a third party.

    Names stay — they cannot be told from ordinary words reliably, and the translation of a
    name is part of what is being judged.
    """
    return _EMAIL_RE.sub("[email]", _PHONE_RE.sub("[phone]", text or ""))


def sample_key(sample: dict) -> str:
    """Identity of a sample across evaluators: direct translations and events share ids."""
    return f"{sample.get('source') or 'event'}:{sample['id']}"


def build_state(sample: dict) -> dict:
    return {
        "task": "Judge a machine translation of a WhatsApp group message.",
        "target_language": sample.get("target_language") or "Unknown",
        "original": mask_pii(sample.get("original_text") or "")[:STATE_TEXT_LIMIT],
        "translation": mask_pii(sample.get("translated_text") or "")[:STATE_TEXT_LIMIT],
    }


def build_questions() -> dict:
    from typesafe_sdk import Noul, Score

    questions = {
        "quality": Score(
            instructions="Rate the overall quality of the translation of the original into the target language.",
            criteria=QUALITY_LEVELS,
        ),
        "accuracy": Score(
            instructions="How well does the translation preserve the meaning of the original?",
            criteria=ACCURACY_LEVELS,
        ),
        "naturalness": Score(
            instructions="How natural does the translation read to a native speaker of the target language?",
            criteria=NATURALNESS_LEVELS,
        ),
    }
    for issue, statement in ISSUE_STATEMENTS.items():
        questions[f"issue_{issue}"] = Noul(instructions=statement)
    return questions


def _level(expected: float) -> int:
    """Jev's expected score is 0-based and fractional; the table stores 1-5 integers."""
    return max(1, min(5, round(expected + 1)))


def parse_response(response, sample: dict) -> dict:
    """Turn a Jev response into the evaluation shape the LLM judge produces."""
    scores = response.scores
    nouls = response.nouls
    quality = scores["quality"]
    issues = [
        {"type": issue, "detail": "", "p": round(nouls[f"issue_{issue}"].noul, 3)}
        for issue in ISSUE_STATEMENTS
        if nouls[f"issue_{issue}"].noul >= JEV_ISSUE_THRESHOLD
    ]
    return {
        "evaluator": "jev",
        "sample_key": sample_key(sample),
        "message_event_id": sample["id"] if sample.get("source") != "direct" else None,
        "original_text": sample.get("original_text") or "",
        "translated_text": sample.get("translated_text") or "",
        "target_language": sample.get("target_language") or "Unknown",
        "quality_score": _level(quality.score),
        "accuracy_score": _level(scores["accuracy"].score),
        "naturalness_score": _level(scores["naturalness"].score),
        "quality_expected": round(quality.score + 1, 3),
        "confidence": round(quality.confidence, 3),
        "issues": issues,
    }


class JevUnavailable(RuntimeError):
    """Jev cannot be used at all this run (no key, rejected key) — fall back to the LLM."""


def evaluate_samples(samples: list[dict], client=None, log=None) -> dict:
    """Score every sample with Jev, one request per translation.

    A bad request skips that sample; a rejected key or a run of failures aborts with
    JevUnavailable so the caller can fall back to the LLM judge instead of storing a
    half-empty night.
    """
    from typesafe_sdk import (
        TypeSafeAuthenticationError,
        TypeSafeClient,
        TypeSafeError,
        TypeSafePermissionDeniedError,
    )

    if client is None:
        if not TYPESAFE_API_KEY:
            raise JevUnavailable("TYPESAFE_API_KEY is not set")
        client = TypeSafeClient(model=JEV_MODEL, timeout=JEV_TIMEOUT)

    questions = build_questions()
    evaluations: list[dict] = []
    failed = 0
    in_a_row = 0
    tokens = 0
    with client:
        for sample in samples:
            try:
                response = client.system_one(state=build_state(sample), questions=questions)
            except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
                raise JevUnavailable(f"Jev rejected the API key: {exc}") from exc
            except TypeSafeError as exc:
                failed += 1
                in_a_row += 1
                if log:
                    log.warning("Jev failed on %s: %s", sample_key(sample), exc)
                if in_a_row >= JEV_MAX_CONSECUTIVE_FAILURES:
                    raise JevUnavailable(f"{in_a_row} Jev requests failed in a row: {exc}") from exc
                continue
            in_a_row = 0
            evaluations.append(parse_response(response, sample))
            usage = response.usage
            tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0)

    return {"evaluations": evaluations, "failed": failed, "tokens_used": tokens}


def pick_worst(evaluations: list[dict], limit: int) -> list[dict]:
    """The translations worth an LLM's written explanation: low quality or a likely issue."""
    flagged = [
        ev for ev in evaluations
        if ev["quality_expected"] <= JEV_BAD_QUALITY or ev["issues"]
    ]
    return sorted(flagged, key=lambda ev: ev["quality_expected"])[:limit]


def agreement(llm_evals: list[dict], jev_evals: list[dict]) -> dict:
    """How closely Jev tracks the LLM judge on the translations both scored."""
    jev_by_key = {ev["sample_key"]: ev for ev in jev_evals}
    pairs = [
        (llm, jev_by_key[llm["sample_key"]])
        for llm in llm_evals
        if llm.get("sample_key") in jev_by_key and llm.get("quality_score")
    ]
    if not pairs:
        return {"n": 0}

    diffs = [abs(llm["quality_score"] - jev["quality_expected"]) for llm, jev in pairs]
    llm_bad = [llm["quality_score"] <= 3 for llm, _ in pairs]
    jev_bad = [jev["quality_expected"] <= JEV_BAD_QUALITY for _, jev in pairs]
    both_bad = sum(a and b for a, b in zip(llm_bad, jev_bad))

    llm_issues = {(llm["sample_key"], i.get("type")) for llm, _ in pairs for i in llm.get("issues", [])}
    jev_issues = {(jev["sample_key"], i["type"]) for _, jev in pairs for i in jev["issues"]}
    common = len(llm_issues & jev_issues)

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 3) if b else None

    return {
        "n": len(pairs),
        "mae": round(sum(diffs) / len(diffs), 3),
        "exact": ratio(sum(llm["quality_score"] == jev["quality_score"] for llm, jev in pairs), len(pairs)),
        "within_1": ratio(sum(d <= 1 for d in diffs), len(pairs)),
        "bad_llm": sum(llm_bad),
        "bad_precision": ratio(both_bad, sum(jev_bad)),
        "bad_recall": ratio(both_bad, sum(llm_bad)),
        "issue_precision": ratio(common, len(jev_issues)),
        "issue_recall": ratio(common, len(llm_issues)),
    }
