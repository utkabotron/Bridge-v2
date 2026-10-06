"""Jev scoring for translation_quality: response parsing, agreement, failure handling."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from flows import jev_eval


def _score(expected: float, confidence: float = 0.8):
    return SimpleNamespace(score=expected, confidence=confidence)


def _response(quality: float, issues: dict[str, float] | None = None, accuracy: float = 4, naturalness: float = 4):
    issues = issues or {}
    return SimpleNamespace(
        scores={"quality": _score(quality), "accuracy": _score(accuracy), "naturalness": _score(naturalness)},
        nouls={f"issue_{i}": SimpleNamespace(noul=issues.get(i, 0.05)) for i in jev_eval.ISSUE_STATEMENTS},
        usage=SimpleNamespace(input_tokens=100, output_tokens=5),
    )


SAMPLE = {"id": 7, "original_text": "שלום", "translated_text": "Привет", "target_language": "Russian"}


def test_mask_pii_hides_phones_and_emails_but_keeps_words():
    text = "Звоните Линор +972 54-449 5021 или пишите lin@mail.co.il, урок в 16:00"
    masked = jev_eval.mask_pii(text)
    assert "449" not in masked and "lin@" not in masked
    assert "[phone]" in masked and "[email]" in masked
    assert "Линор" in masked and "16:00" in masked


def test_sample_key_keeps_direct_and_event_ids_apart():
    assert jev_eval.sample_key({"id": 5}) != jev_eval.sample_key({"id": 5, "source": "direct"})


def test_parse_response_maps_jev_scale_onto_the_llm_one():
    """Jev's score is 0-based and fractional; the table stores the LLM's 1-5 integers."""
    ev = jev_eval.parse_response(_response(3.6, accuracy=0.2, naturalness=2.4), SAMPLE)

    assert ev["quality_expected"] == 4.6
    assert ev["quality_score"] == 5
    assert ev["accuracy_score"] == 1
    assert ev["naturalness_score"] == 3
    assert ev["evaluator"] == "jev"
    assert ev["message_event_id"] == 7


def test_parse_response_keeps_only_likely_issues():
    ev = jev_eval.parse_response(_response(1.0, {"omission": 0.91, "grammar": 0.3}), SAMPLE)
    assert [i["type"] for i in ev["issues"]] == ["omission"]


def test_direct_translation_has_no_message_event():
    ev = jev_eval.parse_response(_response(4.0), {**SAMPLE, "source": "direct"})
    assert ev["message_event_id"] is None


def _jev(key, expected, issues=()):
    return {
        "sample_key": key,
        "quality_expected": expected,
        "quality_score": jev_eval._level(expected - 1),
        "issues": [{"type": t} for t in issues],
    }


def test_pick_worst_takes_low_scores_and_flagged_issues_worst_first():
    evals = [_jev("a", 4.8), _jev("b", 2.1), _jev("c", 4.5, ["omission"]), _jev("d", 3.0)]
    assert [e["sample_key"] for e in jev_eval.pick_worst(evals, 10)] == ["b", "d", "c"]
    assert len(jev_eval.pick_worst(evals, 2)) == 2


def test_agreement_on_overlapping_translations_only():
    llm = [
        {"sample_key": "a", "quality_score": 5, "issues": []},
        {"sample_key": "b", "quality_score": 2, "issues": [{"type": "omission"}]},
        {"sample_key": "z", "quality_score": 1, "issues": []},  # Jev never saw it
    ]
    jev = [_jev("a", 4.6), _jev("b", 2.4, ["omission"])]

    result = jev_eval.agreement(llm, jev)

    assert result["n"] == 2
    assert result["mae"] == 0.4
    assert result["within_1"] == 1.0
    assert result["bad_recall"] == 1.0 and result["bad_precision"] == 1.0
    assert result["issue_recall"] == 1.0


def test_agreement_without_overlap_is_empty():
    assert jev_eval.agreement([{"sample_key": "x", "quality_score": 3}], []) == {"n": 0}


# ── evaluate_samples needs the SDK's exception types ──

class _FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def system_one(self, state, questions):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _samples(n):
    return [{**SAMPLE, "id": i} for i in range(n)]


def test_evaluate_samples_skips_a_failed_request():
    sdk = pytest.importorskip("typesafe_sdk")
    client = _FakeClient([_response(4.0), sdk.TypeSafeError("bad input"), _response(2.0)])

    result = jev_eval.evaluate_samples(_samples(3), client=client)

    assert [e["message_event_id"] for e in result["evaluations"]] == [0, 2]
    assert result["failed"] == 1
    assert result["tokens_used"] == 210


def test_rejected_key_aborts_so_the_flow_falls_back_to_the_llm():
    pytest.importorskip("typesafe_sdk")
    import httpx2
    from typesafe_sdk import TypeSafeAuthenticationError

    client = _FakeClient([TypeSafeAuthenticationError(401, {"error": "invalid key"}, httpx2.Headers())])

    with pytest.raises(jev_eval.JevUnavailable):
        jev_eval.evaluate_samples(_samples(3), client=client)


def test_an_outage_stops_after_a_run_of_failures_instead_of_trying_every_sample():
    sdk = pytest.importorskip("typesafe_sdk")
    limit = jev_eval.JEV_MAX_CONSECUTIVE_FAILURES
    client = _FakeClient([sdk.TypeSafeError("down")] * 50)

    with pytest.raises(jev_eval.JevUnavailable):
        jev_eval.evaluate_samples(_samples(50), client=client)
    assert client.calls == limit


def test_missing_key_is_unavailable_not_a_crash(monkeypatch):
    pytest.importorskip("typesafe_sdk")
    monkeypatch.setattr(jev_eval, "TYPESAFE_API_KEY", "")
    with pytest.raises(jev_eval.JevUnavailable):
        jev_eval.evaluate_samples(_samples(1))


def test_questions_cover_every_llm_issue_type():
    pytest.importorskip("typesafe_sdk")
    questions = jev_eval.build_questions()
    assert {"quality", "accuracy", "naturalness"} <= questions.keys()
    assert {f"issue_{i}" for i in jev_eval.ISSUE_STATEMENTS} <= questions.keys()
