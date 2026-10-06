from flows import quality_stats as qs


def _ev(score, source="bridge", pair=1, lang="Russian", mtype="chat", version="v2.10"):
    return {"quality_score": score, "source": source, "chat_pair_id": pair,
            "target_language": lang, "message_type": mtype, "prompt_version": version}


EVALS = [
    _ev(5), _ev(4), _ev(2),                      # pair 1: one bad of three
    _ev(2, pair=29), _ev(3, pair=29), _ev(5, pair=29), _ev(1, pair=29),  # pair 29: three bad of four
    _ev(1, source="fallback", pair=None),        # must not drag the bridge numbers down
    _ev(5, source="direct", pair=None, lang="Hebrew"),
    _ev(4, mtype="image", version="v2.9"),
]


def test_bridge_slice_excludes_fallback_and_direct():
    assert len(qs.bridge_only(EVALS)) == 8
    bd = qs.quality_breakdown(EVALS)
    assert set(bd["by_source"]) == {"bridge", "fallback", "direct"}
    assert bd["by_source"]["fallback"]["quality"] == 1.0
    assert bd["by_source"]["bridge"]["n"] == 8


def test_pairs_sorted_worst_first_and_named():
    bd = qs.quality_breakdown(EVALS)
    assert bd["by_pair"][0]["key"] == 29
    assert bd["by_pair"][0]["bad_pct"] == 75.0
    assert bd["by_pair"][0]["quality"] == 2.75
    assert qs.worst_pair(bd, min_n=4)["key"] == 29
    assert qs.worst_pair(bd, min_n=5) is None  # not enough samples to point a finger


def test_language_type_and_prompt_slices_are_bridge_only():
    bd = qs.quality_breakdown(EVALS)
    assert [r["key"] for r in bd["by_language"]] == ["Russian"]  # Hebrew DM is not in
    assert {r["key"] for r in bd["by_type"]} == {"chat", "image"}
    assert {r["key"] for r in bd["by_prompt_version"]} == {"v2.10", "v2.9"}


def test_missing_scores_do_not_crash():
    bd = qs.quality_breakdown([{"source": "bridge", "chat_pair_id": 3}])
    assert bd["by_source"]["bridge"] == {"n": 0, "quality": None, "bad": 0, "bad_pct": None}
    assert bd["by_pair"] == [{"key": 3, "n": 0, "quality": None, "bad": 0, "bad_pct": None}]
