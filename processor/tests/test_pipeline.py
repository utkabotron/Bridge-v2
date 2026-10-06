"""Unit tests for the pipeline nodes and their order."""
from __future__ import annotations

import os

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def _completion(text: str):
    """What src/llm.chat returns, minus the bookkeeping the nodes do not read."""
    from processor.src.llm import Completion
    return Completion(text=text, model="test-model")


def _base_state(**overrides):
    state = {
        "wa_message_id": "test-123",
        "wa_chat_id": "1234567890@g.us",
        "wa_chat_name": "Test Group",
        "user_id": 100,
        "sender_name": "Alice",
        "original_text": "שלום, מה שלומך?",
        "message_type": "text",
        "media_s3_url": None,
        "timestamp": 1700000000,
        "from_me": False,
        "is_edited": False,
        "chat_pair_id": None,
        "tg_chat_id": None,
        "target_language": "Russian",
        "translated_text": None,
        "translation_ms": None,
        "cache_hit": False,
        "formatted_text": None,
        "delivery_status": "pending",
        "error": None,
    }
    state.update(overrides)
    return state


# ── validate_node ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_node_no_pair():
    """When no chat pair exists, delivery_status should be 'skipped'."""
    from processor.src.pipeline.nodes import validate_node

    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=AsyncMock(return_value=[])):
        result = await validate_node(_base_state())

    assert result["delivery_status"] == "skipped"
    assert result["error"] == "no_chat_pair"


@pytest.mark.asyncio
async def test_validate_node_with_pair():
    """When a chat pair exists, tg_chat_id and target_language are set."""
    from processor.src.pipeline.nodes import validate_node

    pair = {"id": 7, "tg_chat_id": -1001234567890, "target_language": "Hebrew"}
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=AsyncMock(return_value=[pair])):
        result = await validate_node(_base_state())

    assert result["chat_pair_id"] == 7
    assert result["tg_chat_id"] == -1001234567890
    assert result["target_language"] == "Hebrew"
    assert result["delivery_status"] == "pending"


@pytest.mark.asyncio
async def test_validate_node_pair_already_resolved():
    """A pre-resolved pair (fan-out branch) passes through without a second DB query."""
    from processor.src.pipeline.nodes import validate_node

    fetch = AsyncMock(return_value=[])
    state = _base_state(chat_pair_id=9, tg_chat_id=-100999, target_language="Hebrew")
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=fetch):
        result = await validate_node(state)

    fetch.assert_not_awaited()
    assert result["chat_pair_id"] == 9
    assert result["tg_chat_id"] == -100999


@pytest.mark.asyncio
async def test_validate_node_does_not_look_up_pairs_the_consumer_already_found_empty():
    """The consumer's "no pair" answer is final — validate must not ask again."""
    from processor.src.pipeline.nodes import validate_node

    lookup = AsyncMock(return_value=[{"id": 1, "tg_chat_id": -1, "target_language": "Hebrew"}])
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=lookup):
        result = await validate_node(_base_state(pairs_resolved=True))

    lookup.assert_not_awaited()
    assert result["delivery_status"] == "skipped"
    assert result["error"] == "no_chat_pair"


@pytest.mark.asyncio
async def test_validate_node_skips_unpaired_admin_chat_by_default():
    """With the fallback off, an unpaired admin chat is skipped — media included."""
    from processor.src.pipeline.nodes import validate_node

    state = _base_state(user_id=100, original_text="", message_type="video",
                        media_s3_url="https://s3/bridge-media/vid.mp4")
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.pipeline.nodes.ADMIN_NO_PAIR_FALLBACK", False), \
         patch.dict(os.environ, {"ADMIN_TG_IDS": "100"}):
        result = await validate_node(state)

    assert result.get("fallback_to_admins") is not True
    assert result["delivery_status"] == "skipped"
    assert result["error"] == "no_chat_pair"


@pytest.mark.asyncio
async def test_validate_node_forwards_captionless_media_when_fallback_enabled():
    """With the fallback on, a caption-less video from an admin chat is forwarded."""
    from processor.src.pipeline.nodes import validate_node

    state = _base_state(user_id=100, original_text="", message_type="video",
                        media_s3_url="https://s3/bridge-media/vid.mp4")
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.pipeline.nodes.ADMIN_NO_PAIR_FALLBACK", True), \
         patch.dict(os.environ, {"ADMIN_TG_IDS": "100"}):
        result = await validate_node(state)

    assert result.get("fallback_to_admins") is True
    assert result.get("delivery_status") != "skipped"


@pytest.mark.asyncio
async def test_validate_node_skips_russian_text_without_media_from_admin_chat():
    """Even with the fallback on, Russian-only text with no media gets skipped."""
    from processor.src.pipeline.nodes import validate_node

    state = _base_state(user_id=100, message_type="text", media_s3_url=None,
                        original_text="привет как дела у тебя сегодня всё хорошо надеюсь")
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.pipeline.nodes.ADMIN_NO_PAIR_FALLBACK", True), \
         patch.dict(os.environ, {"ADMIN_TG_IDS": "100"}):
        result = await validate_node(state)

    assert result.get("fallback_to_admins") is not True
    assert result["delivery_status"] == "skipped"


@pytest.mark.parametrize("text,is_russian", [
    ("привет как дела", True),
    ("", True),            # caption-less media: the media gate decides, not the language
    ("👍", True),          # no letters, nothing worth forwarding
    ("12:30", True),
    ("hello how are you", False),
    ("שלום, מה שלומך?", False),
    ("привет, מה שלומך", False),  # mixed with a source script still gets forwarded
])
def test_admin_fallback_language_gate(text, is_russian):
    from processor.src.pipeline.nodes import _is_russian_text

    assert _is_russian_text(text) is is_russian


@pytest.mark.asyncio
async def test_validate_node_forwards_non_russian_text_when_fallback_enabled():
    from processor.src.pipeline.nodes import validate_node

    state = _base_state(user_id=100, original_text="hello how are you today")
    with patch("processor.src.pipeline.nodes.lookup_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.pipeline.nodes.ADMIN_NO_PAIR_FALLBACK", True), \
         patch.dict(os.environ, {"ADMIN_TG_IDS": "100"}):
        result = await validate_node(state)

    assert result.get("fallback_to_admins") is True


# ── translate_node ────────────────────────────────────────

@pytest.mark.asyncio
async def test_translate_node_cache_hit():
    """Cache hit should return translation without calling LLM."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian")

    with patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value="Привет, как дела?")), \
         patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value={})), \
         patch("processor.src.pipeline.nodes.get_cached_global", new=AsyncMock(return_value=None)), \
         patch("processor.src.db.fetch_chat_profile", new=AsyncMock(return_value=None)):
        result = await translate_node(state)

    assert result["translated_text"] == "Привет, как дела?"
    assert result["cache_hit"] is True
    assert result["translation_ms"] == 0


async def _translate_with_profile_cache(cached_profile, db_profile):
    """Run translate_node (cache hit, so no LLM) and return the profile-cache calls."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian")
    fetch = AsyncMock(return_value=db_profile)
    store = AsyncMock()
    with patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value=cached_profile)), \
         patch("processor.src.pipeline.nodes.set_chat_profile", new=store), \
         patch("processor.src.db.fetch_chat_profile", new=fetch), \
         patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value="Привет")):
        await translate_node(state)
    return fetch, store


@pytest.mark.asyncio
async def test_translate_node_caches_the_absence_of_a_profile():
    """Most pairs have no chat_profiles row; without a cached "none" each message hit Postgres."""
    fetch, store = await _translate_with_profile_cache(cached_profile=None, db_profile=None)

    fetch.assert_awaited_once_with(1)
    store.assert_awaited_once_with(1, {})


@pytest.mark.asyncio
async def test_translate_node_trusts_a_cached_empty_profile():
    fetch, store = await _translate_with_profile_cache(cached_profile={}, db_profile={"tone": "x"})

    fetch.assert_not_awaited()
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_translate_node_caches_a_profile_it_loaded():
    profile = {"tone": "casual", "glossary": {"שלום": "привет"}}
    fetch, store = await _translate_with_profile_cache(cached_profile=None, db_profile=profile)

    fetch.assert_awaited_once_with(1)
    store.assert_awaited_once_with(1, profile)


@pytest.mark.asyncio
async def test_translate_node_llm_call():
    """Cache miss should call LLM and store result."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian")

    with patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.get_cached_global", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.set_cached", new=AsyncMock()), \
         patch("processor.src.pipeline.nodes.set_cached_global", new=AsyncMock()), \
         patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value={})), \
         patch("processor.src.db.fetch_chat_profile", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.llm_chat", new=AsyncMock(return_value=_completion("Привет, как дела?"))) as chat:
        result = await translate_node(state)

    assert result["translated_text"] == "Привет, как дела?"
    assert result["cache_hit"] is False
    assert result["translation_ms"] >= 0
    messages = chat.await_args.args[0]
    assert messages[0]["role"] == "system" and messages[1] == {"role": "user", "content": state["original_text"].strip()}
    assert chat.await_args.kwargs["purpose"] == "translate"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on,pair,expected_version,expected_model", [
    (True, 29, "v2.10@gpt-6-luna", "gpt-6-luna"),
    (True, 12, "v2.10", "default"),
    (False, 29, "v2.10", "default"),
])
async def test_translate_node_runs_the_ab_variant_and_records_its_version(flag_on, pair, expected_version, expected_model):
    """Odd pairs get variant B while the flag is on; the version rides along for the evaluation."""
    from processor.src.pipeline.nodes import translate_node

    from processor.src.config import OPENAI_MODEL
    if expected_model == "default":
        expected_model = OPENAI_MODEL

    state = _base_state(chat_pair_id=pair, tg_chat_id=-100, target_language="Russian")

    with patch("processor.src.feature_flags.is_enabled", new=AsyncMock(return_value=flag_on)), \
         patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value=None)) as get_cached, \
         patch("processor.src.pipeline.nodes.get_cached_global", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.set_cached", new=AsyncMock()) as set_cached, \
         patch("processor.src.pipeline.nodes.set_cached_global", new=AsyncMock()), \
         patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value={})), \
         patch("processor.src.db.fetch_chat_profile", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.llm_chat", new=AsyncMock(return_value=_completion("Привет"))) as chat:
        result = await translate_node(state)

    assert result["prompt_version"] == expected_version
    assert chat.await_args.kwargs["model"] == expected_model
    # The version is recorded with the call, so llm_usage splits cost by A/B variant.
    assert chat.await_args.kwargs["tag"] == expected_version
    # The cache is partitioned by version, so A and B never serve each other's translations.
    assert get_cached.await_args.kwargs["version"] == expected_version
    assert set_cached.await_args.kwargs["version"] == expected_version


def test_looks_untranslated_detects_echo_but_not_real_translation():
    """The passthrough guard flags echoed source, not legitimate translations."""
    from processor.src.pipeline.nodes import _looks_untranslated

    heb = "שלום חברים"
    assert _looks_untranslated(heb, heb, "Russian") is True             # exact echo
    assert _looks_untranslated(heb, heb + "  ", "Russian") is True      # whitespace-only diff
    assert _looks_untranslated(heb, "שלום לכם", "Russian") is True      # still Hebrew, no target script
    assert _looks_untranslated(heb, "Привет, друзья", "Russian") is False   # real translation
    assert _looks_untranslated(heb, "Привет שלום", "Russian") is False      # mixed, has target script
    assert _looks_untranslated(heb, "שלום", "Klingon") is False         # unknown target → cannot judge
    assert _looks_untranslated(heb, "", "Russian") is False             # empty handled elsewhere


@pytest.mark.asyncio
async def test_translate_node_retries_when_model_echoes_source():
    """A first reply that repeats the Hebrew source triggers one corrective retry."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian",
                        original_text="שלום, מה שלומך?")
    chat = AsyncMock(side_effect=[_completion("שלום, מה שלומך?"), _completion("Привет, как дела?")])

    with patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.get_cached_global", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.set_cached", new=AsyncMock()) as set_cached, \
         patch("processor.src.pipeline.nodes.set_cached_global", new=AsyncMock()), \
         patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value={})), \
         patch("processor.src.db.fetch_chat_profile", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.llm_chat", new=chat):
        result = await translate_node(state)

    assert chat.await_count == 2
    # The retry replays the echo as the assistant turn and asks again
    retry = chat.await_args_list[1].args[0]
    assert retry[-2] == {"role": "assistant", "content": "שלום, מה שלומך?"}
    assert retry[-1]["role"] == "user" and "NOT translated" in retry[-1]["content"]
    assert chat.await_args_list[1].kwargs["purpose"] == "translate_retry"
    assert result["translated_text"] == "Привет, как дела?"
    assert result["translation_passthrough"] is False
    set_cached.assert_awaited()  # the corrected result is cache-worthy


@pytest.mark.asyncio
async def test_translate_node_flags_and_skips_cache_on_persistent_passthrough():
    """When the model refuses to translate even after the retry, deliver as-is but never cache."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian",
                        original_text="שלום, מה שלומך?")
    chat = AsyncMock(side_effect=[_completion("שלום, מה שלומך?"), _completion("שלום, מה שלומך?")])

    with patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.get_cached_global", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.set_cached", new=AsyncMock()) as set_cached, \
         patch("processor.src.pipeline.nodes.set_cached_global", new=AsyncMock()) as set_cached_global, \
         patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value={})), \
         patch("processor.src.db.fetch_chat_profile", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.llm_chat", new=chat):
        result = await translate_node(state)

    assert chat.await_count == 2
    assert result["translated_text"] == "שלום, מה שלומך?"
    assert result["translation_passthrough"] is True
    set_cached.assert_not_awaited()
    set_cached_global.assert_not_awaited()


@pytest.mark.asyncio
async def test_translate_node_empty_text():
    """Empty text should be passed through without LLM call."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(original_text="", target_language="Russian")
    result = await translate_node(state)

    assert result["translated_text"] == ""
    assert result["translation_ms"] == 0


# ── format_node ───────────────────────────────────────────

def test_format_node_with_sender():
    from processor.src.pipeline.nodes import format_node

    state = _base_state(
        sender_name="Bob",
        wa_chat_name="Team Chat",
        translated_text="Hello world",
        chat_pair_id=1,
        tg_chat_id=-100,
    )
    result = format_node(state)
    assert "Bob" in result["formatted_text"]
    assert "Hello world" in result["formatted_text"]


def test_format_node_with_media():
    """Media URL is no longer embedded in formatted_text — sent natively via Telegram API."""
    from processor.src.pipeline.nodes import format_node

    state = _base_state(
        translated_text="Check this out",
        media_s3_url="https://s3.amazonaws.com/bucket/file.jpg",
    )
    result = format_node(state)
    # Media link removed from formatted text — delivered natively by telegram_sender
    assert "s3.amazonaws.com" not in result["formatted_text"]
    assert "Check this out" in result["formatted_text"]


# ── graph routing ─────────────────────────────────────────

def test_should_translate_routing():
    from processor.src.pipeline.graph import _should_translate

    # Failed or skipped state → go to deliver
    assert _should_translate({"delivery_status": "failed", "original_text": "text"}) == "deliver"
    assert _should_translate({"delivery_status": "skipped", "original_text": "text"}) == "deliver"

    # No text → skip translation
    assert _should_translate({"delivery_status": "pending", "original_text": ""}) == "format"

    # Normal → translate
    assert _should_translate({"delivery_status": "pending", "original_text": "hello"}) == "translate"


@pytest.mark.asyncio
@pytest.mark.parametrize("routed,expected_steps", [
    ("translate", ["validate", "translate", "format", "deliver"]),
    ("format", ["validate", "format", "deliver"]),
    ("deliver", ["validate", "deliver"]),
])
async def test_pipeline_runs_the_steps_the_route_asks_for(routed, expected_steps):
    """LangGraph used to run these; the plain pipeline keeps its {node: output} stream."""
    from processor.src.pipeline import graph

    calls = []

    def step(name, *, is_async=True):
        def record(state):
            calls.append(name)
            return {**state, f"seen_{name}": True}

        async def record_async(state):
            return record(state)
        return record_async if is_async else record

    steps = {
        "validate": step("validate"),
        "translate": step("translate"),
        "format": step("format", is_async=False),  # format_node is synchronous
        "deliver": step("deliver"),
    }
    with patch.object(graph.Pipeline, "steps", steps), \
         patch.object(graph, "_should_translate", return_value=routed):
        chunks = [chunk async for chunk in graph.pipeline.astream(_base_state())]

    assert calls == expected_steps
    assert [next(iter(c)) for c in chunks] == expected_steps
    final = next(iter(chunks[-1].values()))
    assert all(final.get(f"seen_{s}") for s in expected_steps)  # each step saw the previous ones


@pytest.mark.asyncio
async def test_pipeline_merges_partial_node_outputs():
    from processor.src.pipeline import graph

    async def partial(state):
        return {"delivery_status": "delivered"}

    steps = {name: partial for name in ("validate", "translate", "format", "deliver")}
    with patch.object(graph.Pipeline, "steps", steps), \
         patch.object(graph, "_should_translate", return_value="deliver"):
        final = await graph.pipeline.ainvoke(_base_state())
    assert final["delivery_status"] == "delivered"
    assert final["original_text"] == _base_state()["original_text"]


@pytest.mark.parametrize("text", ["👍", "❤️🙏🏻", "🎉🎉🎉", "15:00", "https://example.com/x"])
def test_nothing_to_translate_skips_the_llm(text):
    """A lone 👍 fell outside the old emoji regex and cost two LLM calls to come back as 👍."""
    from processor.src.pipeline.graph import _should_translate

    assert _should_translate({"delivery_status": "pending", "original_text": text}) == "format"


@pytest.mark.parametrize("text", ["תודה 👍", "ок", "Eran the King 👑"])
def test_words_next_to_emoji_still_translate(text):
    from processor.src.pipeline.graph import _should_translate

    state = {"delivery_status": "pending", "original_text": text, "target_language": "Hebrew"}
    assert _should_translate(state) == "translate"


# ── fan-out over chat pairs ───────────────────────────────

@pytest.mark.asyncio
async def test_process_message_fans_out_to_every_pair():
    """A group message runs once per active pair of that chat.

    Dedup in wa-service keeps a single copy of a group message, so delivering it to just
    one pair would starve every other user bridging the same WhatsApp group.
    """
    import json

    import processor.src.main as main

    payload = {
        "wa_message_id": "fallback:abc",
        "wa_chat_id": "1234567890@g.us",
        "user_id": 100,
        "body": "שלום",
    }
    pairs = [
        {"id": 11, "tg_chat_id": -1001, "target_language": "Russian"},
        {"id": 15, "tg_chat_id": -1002, "target_language": "Hebrew"},
    ]
    runs = []

    async def fake_run(r, payload_, state, msg_id, wa_message_id):
        runs.append((state["chat_pair_id"], state["tg_chat_id"], state["target_language"]))

    delivered = AsyncMock(return_value=set())
    with patch("processor.src.main.lookup_chat_pairs", new=AsyncMock(return_value=pairs)), \
         patch("processor.src.main.fetch_delivered_pair_ids", new=delivered), \
         patch("processor.src.main._run_pipeline", new=fake_run):
        await main._process_message(MagicMock(), json.dumps(payload))

    assert runs == [(11, -1001, "Russian"), (15, -1002, "Hebrew")]
    # A fresh message costs no dedup query at all — wa-service dedup already covers it.
    delivered.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["_dlq_attempts", "_requeued"])
async def test_requeued_message_skips_pairs_it_already_reached(marker):
    """A fan-out that failed halfway comes back through the DLQ (or the in-flight list).

    The pair that got its copy must be skipped, the other still runs — the upsert guard in
    insert_message_event only protects the row, after Telegram was already called, so
    without this check the first pair would get the message twice.
    """
    import json

    import processor.src.main as main

    payload = {"wa_message_id": "fallback:abc", "wa_chat_id": "123@g.us", "user_id": 100, "body": "hi",
               marker: 1}
    pairs = [
        {"id": 11, "tg_chat_id": -1001, "target_language": "Russian"},
        {"id": 15, "tg_chat_id": -1002, "target_language": "Russian"},
    ]
    runs = []

    async def fake_run(r, payload_, state, msg_id, wa_message_id):
        runs.append(state["chat_pair_id"])

    delivered = AsyncMock(return_value={11})
    with patch("processor.src.main.lookup_chat_pairs", new=AsyncMock(return_value=pairs)), \
         patch("processor.src.main.fetch_delivered_pair_ids", new=delivered), \
         patch("processor.src.main._run_pipeline", new=fake_run):
        await main._process_message(MagicMock(), json.dumps(payload))

    assert runs == [15]
    delivered.assert_awaited_once_with("fallback:abc")  # one query for the whole fan-out


@pytest.mark.asyncio
async def test_requeued_pairless_message_is_not_delivered_to_admins_twice():
    """The pair-less run (admin fallback) is recorded under chat_pair_id NULL; same dedup."""
    import json

    import processor.src.main as main

    payload = {"wa_message_id": "fallback:abc", "wa_chat_id": "123@c.us", "user_id": 100, "body": "hi",
               "_dlq_attempts": 1}
    runs = []

    async def fake_run(r, payload_, state, msg_id, wa_message_id):
        runs.append(state["chat_pair_id"])

    with patch("processor.src.main.lookup_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.main.fetch_delivered_pair_ids", new=AsyncMock(return_value={None})), \
         patch("processor.src.main._run_pipeline", new=fake_run):
        await main._process_message(MagicMock(), json.dumps(payload))

    assert runs == []


@pytest.mark.asyncio
async def test_inflight_recovery_flags_messages_for_the_dedup_check():
    import json

    import processor.src.main as main

    raw = json.dumps({"wa_message_id": "m1", "body": "hi"})
    r = MagicMock()
    r.lrange = AsyncMock(return_value=[raw, "not json"])
    r.rpush = AsyncMock()
    r.lrem = AsyncMock()

    await main._requeue_inflight(r)

    pushed = [call.args[1] for call in r.rpush.await_args_list]
    assert json.loads(pushed[0]) == {"wa_message_id": "m1", "body": "hi", "_requeued": True}
    assert pushed[1] == "not json"  # unparseable: handed back untouched, as before
    assert [call.args[2] for call in r.lrem.await_args_list] == [raw, "not json"]


@pytest.mark.asyncio
async def test_process_message_without_pairs_still_runs_once():
    """No pair at all → one pair-less run, so validate_node decides skip vs admin fallback."""
    import json

    import processor.src.main as main

    payload = {"wa_message_id": "fallback:abc", "wa_chat_id": "123@g.us", "user_id": 100, "body": "hi"}
    runs = []
    resolved = []

    async def fake_run(r, payload_, state, msg_id, wa_message_id):
        runs.append(state["chat_pair_id"])
        resolved.append(state.get("pairs_resolved"))

    with patch("processor.src.main.lookup_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.main._run_pipeline", new=fake_run):
        await main._process_message(MagicMock(), json.dumps(payload))

    assert runs == [None]
    assert resolved == [True]  # validate_node is told the lookup already happened


# ── Pair cache is invalidated by the changes the processor makes itself ──

@pytest.mark.asyncio
async def test_pausing_a_dead_chat_drops_the_cached_pairs():
    """Otherwise every later message retries the dead chat until the cache entry expires."""
    from processor.src.pipeline.nodes import _pause_dead_chat

    state = _base_state(chat_pair_id=7, user_id=100, wa_chat_id="123@g.us")
    invalidate = AsyncMock()
    with patch("processor.src.pipeline.nodes.invalidate_chat_pairs", new=invalidate), \
         patch("processor.src.telegram_sender.is_dead_chat", return_value=True), \
         patch("processor.src.db.pause_chat_pair", new=AsyncMock(return_value=None)):
        await _pause_dead_chat(state, "Bad Request: chat not found")

    invalidate.assert_awaited_once_with(100, "123@g.us")


@pytest.mark.asyncio
async def test_supergroup_migration_drops_the_cached_pairs():
    from processor.src.pipeline.nodes import _migrate_chat_pair

    state = _base_state(chat_pair_id=7, user_id=100, wa_chat_id="123@g.us")
    pool = MagicMock()
    pool.execute = AsyncMock()
    invalidate = AsyncMock()
    with patch("processor.src.pipeline.nodes.invalidate_chat_pairs", new=invalidate), \
         patch("processor.src.db.get_pool", new=AsyncMock(return_value=pool)):
        await _migrate_chat_pair(state, -1009999)

    pool.execute.assert_awaited_once()
    invalidate.assert_awaited_once_with(100, "123@g.us")


# ── Resilience: translation failures must not swallow the message ──

@pytest.mark.asyncio
async def test_translate_node_degrades_when_llm_fails():
    """An OpenAI outage used to raise, escape the graph and dead-letter the message.

    Nothing drained that queue, so the bridge simply went quiet. Delivering the original
    untranslated keeps the pipe open.
    """
    from processor.src.pipeline.nodes import translate_node

    with patch("processor.src.pipeline.nodes.llm_chat", new=AsyncMock(side_effect=RuntimeError("openai unavailable"))), \
         patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.get_cached_global", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.get_chat_profile", new=AsyncMock(return_value=None)):
        result = await translate_node(_base_state(chat_pair_id=None))

    assert result["translation_failed"] is True
    assert result["translation_error"] == "openai unavailable"
    assert result["translated_text"] == ""
    assert result["original_text"] == _base_state()["original_text"]  # nothing lost


@pytest.fixture
def translation_alerts():
    """Fresh alert state, with the Telegram send replaced by a mock."""
    import processor.src.main as main

    main._translation_fail_times.clear()
    main._last_translation_alert = None
    with patch("processor.src.main._alert_admins_translation", new=AsyncMock()) as alert:
        yield main, alert
    main._translation_fail_times.clear()
    main._last_translation_alert = None


QUOTA_ERROR = ("Error code: 429 - {'error': {'message': 'You have no credits remaining.', "
               "'code': 'insufficient_quota'}}")


@pytest.mark.asyncio
async def test_empty_openai_balance_alerts_on_the_first_untranslated_message(translation_alerts):
    """On 30.09 the balance ran out and messages went untranslated for 12h unnoticed."""
    import asyncio
    main, alert = translation_alerts

    main._track_translation_failure(QUOTA_ERROR)
    main._track_translation_failure(QUOTA_ERROR)  # within cooldown — no second alert
    await asyncio.sleep(0)

    alert.assert_awaited_once()
    assert alert.await_args.args[1] == QUOTA_ERROR


@pytest.mark.asyncio
async def test_a_transient_translation_error_alerts_only_past_the_threshold(translation_alerts):
    import asyncio
    from processor.src.config import TRANSLATION_FAIL_THRESHOLD
    main, alert = translation_alerts

    for _ in range(TRANSLATION_FAIL_THRESHOLD - 1):
        main._track_translation_failure("Request timed out.")
    await asyncio.sleep(0)
    alert.assert_not_awaited()

    main._track_translation_failure("Request timed out.")
    await asyncio.sleep(0)
    alert.assert_awaited_once()


def test_format_node_marks_an_untranslated_message():
    """The reader should see why a message arrived without its translation."""
    from processor.src.config import TRANSLATION_UNAVAILABLE_NOTE
    from processor.src.pipeline.nodes import format_node

    state = _base_state(translated_text="", translation_failed=True)
    result = format_node(state)

    assert TRANSLATION_UNAVAILABLE_NOTE in result["formatted_text"]
    assert state["original_text"] in result["formatted_text"]


@pytest.mark.asyncio
async def test_failed_pipeline_counts_towards_the_failure_rate():
    """The except branch never called _track_delivery, so an outage raised no alert.

    An OpenAI or Telegram outage lands exactly here, and the failure-rate alert stayed
    silent through the whole incident.
    """
    import processor.src.main as main

    def exploding_astream(*args, **kwargs):
        raise RuntimeError("pipeline exploded")

    fake_pipeline = MagicMock()
    fake_pipeline.astream = exploding_astream

    tracked = []
    dlq = AsyncMock()
    with patch("processor.src.main.pipeline", fake_pipeline), \
         patch("processor.src.main._dlq_push", new=dlq), \
         patch("processor.src.main._track_delivery", side_effect=lambda failed: tracked.append(failed)):
        await main._run_pipeline(MagicMock(), {"wa_message_id": "x"}, _base_state(), "x", "x")

    assert tracked == [True]
    dlq.assert_awaited()  # and the message is still kept for retry


# ── Stage 3: message kinds that used to arrive broken ─────

def test_format_node_flags_media_that_could_not_be_fetched():
    """A failed download used to deliver a bare sender name with no hint of what was lost."""
    from processor.src.pipeline.nodes import format_node

    result = format_node(_base_state(original_text="", message_type="ptt", media_failed=True))

    assert "голосовое сообщение" in result["formatted_text"]


def test_format_node_marks_edits_and_own_messages():
    from processor.src.pipeline.nodes import format_node

    edited = format_node(_base_state(is_edited=True))
    assert "✏️" in edited["formatted_text"]

    own = format_node(_base_state(from_me=True))
    assert "➡️" in own["formatted_text"]


def test_format_node_renders_a_shared_contact_instead_of_raw_vcard():
    from processor.src.pipeline.nodes import format_node

    state = _base_state(
        original_text="",
        message_type="vcard",
        contacts=[{"name": "Dr Cohen", "phones": ["+972-50-123"]}],
    )
    result = format_node(state)

    assert "Dr Cohen" in result["formatted_text"]
    assert "+972-50-123" in result["formatted_text"]
    assert "VCARD" not in result["formatted_text"]


def test_format_node_quotes_the_replied_message_when_threading_is_unavailable():
    """Falls back to an inline preview only when we cannot use a real Telegram reply."""
    from processor.src.pipeline.nodes import format_node

    quoted = {"wa_message_id": "abc", "body": "во сколько встреча?", "sender": "Dana"}

    inline = format_node(_base_state(quoted=quoted))
    assert "во сколько встреча?" in inline["formatted_text"]
    assert "Dana" in inline["formatted_text"]

    threaded = format_node(_base_state(quoted=quoted, reply_to_message_id=555))
    assert "во сколько встреча?" not in threaded["formatted_text"]


# ── Stage 3: not paying to translate what is already readable ──

def test_messages_already_in_the_target_script_skip_the_llm():
    from processor.src.pipeline.graph import _already_in_target_script

    assert _already_in_target_script("Привет, во сколько встреча?", "Russian")
    assert _already_in_target_script("See you at 5", "English")


def test_source_script_or_mixed_text_still_gets_translated():
    from processor.src.pipeline.graph import _already_in_target_script

    assert not _already_in_target_script("מה קורה?", "Russian")
    # Mixed Hebrew/Russian must still go through the model.
    assert not _already_in_target_script("Привет, מה קורה?", "Russian")
    # An unknown target language is never assumed readable.
    assert not _already_in_target_script("Hello", "Thai")


# ── WhatsApp edits rewrite the delivered Telegram message ──

def _edit_state(**overrides):
    """An edit of an already-delivered message, ready for deliver_node."""
    state = _base_state(
        wa_message_id="test-123:edit:9f8e7d",
        is_edited=True,
        chat_pair_id=7,
        tg_chat_id=-100500,
        formatted_text="<b>Alice</b> ✏️ изменено\n\nновый текст",
        formatted_text_plain="<b>Alice</b>\n\nновый текст",
    )
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_edit_rewrites_the_delivered_message_instead_of_sending_a_second_one():
    from processor.src.pipeline.nodes import deliver_node

    edit_message = AsyncMock(return_value=(True, None))
    send_message = AsyncMock()

    with patch("processor.src.db.find_delivered_event", new=AsyncMock(return_value=(4242, 77))), \
         patch("processor.src.db.insert_message_event", new=AsyncMock(return_value=None)), \
         patch("processor.src.telegram_sender.edit_message", new=edit_message), \
         patch("processor.src.telegram_sender.send_message", new=send_message):
        result = await deliver_node(_edit_state())

    send_message.assert_not_called()
    kwargs = edit_message.await_args.kwargs
    assert kwargs["message_id"] == 4242
    assert kwargs["chat_id"] == -100500
    # Telegram adds its own "edited" label, so ours must not be doubled up.
    assert "✏️" not in kwargs["text"]
    assert kwargs["reply_markup"] is None
    assert result["delivery_status"] == "delivered"
    # Points at the message the reader sees, so the next edit lands on it too.
    assert result["tg_message_id"] == 4242


@pytest.mark.asyncio
async def test_edit_falls_back_to_a_new_message_when_telegram_refuses():
    """Too old, deleted, or split in two — the edit must still reach the reader."""
    from processor.src.pipeline.nodes import deliver_node

    send_message = AsyncMock(return_value=(True, None, None, 999))

    with patch("processor.src.db.find_delivered_event", new=AsyncMock(return_value=(4242, 77))), \
         patch("processor.src.db.insert_message_event", new=AsyncMock(return_value=None)), \
         patch("processor.src.telegram_sender.edit_message",
               new=AsyncMock(return_value=(False, "message to edit not found"))), \
         patch("processor.src.telegram_sender.send_message", new=send_message):
        result = await deliver_node(_edit_state())

    kwargs = send_message.await_args.kwargs
    assert kwargs["reply_to_message_id"] == 4242
    assert "✏️" in kwargs["text"]
    assert result["delivery_status"] == "delivered"
    assert result["tg_message_id"] == 999


@pytest.mark.asyncio
async def test_edit_of_a_message_we_never_delivered_is_sent_as_before():
    """Originals from before this feature (or failed ones) have no Telegram message to edit."""
    from processor.src.pipeline.nodes import deliver_node

    edit_message = AsyncMock()
    send_message = AsyncMock(return_value=(True, None, None, 999))

    with patch("processor.src.db.find_delivered_event", new=AsyncMock(return_value=(None, None))), \
         patch("processor.src.db.insert_message_event", new=AsyncMock(return_value=None)), \
         patch("processor.src.telegram_sender.edit_message", new=edit_message), \
         patch("processor.src.telegram_sender.send_message", new=send_message):
        result = await deliver_node(_edit_state())

    edit_message.assert_not_called()
    assert send_message.await_args.kwargs["reply_to_message_id"] is None
    assert "✏️" in send_message.await_args.kwargs["text"]
    assert result["tg_message_id"] == 999


@pytest.mark.asyncio
async def test_editing_a_photo_caption_keeps_the_analyze_button():
    """Telegram strips the inline keyboard on edit unless it is sent again."""
    from processor.src.pipeline.nodes import deliver_node

    edit_message = AsyncMock(return_value=(True, None))

    state = _edit_state(message_type="image", media_s3_url="s3://bridge-media/pic.jpg")

    with patch("processor.src.db.find_delivered_event", new=AsyncMock(return_value=(4242, 77))), \
         patch("processor.src.db.insert_message_event", new=AsyncMock(return_value=None)), \
         patch("processor.src.telegram_sender.edit_message", new=edit_message), \
         patch("processor.src.telegram_sender.send_message", new=AsyncMock()):
        result = await deliver_node(state)

    kwargs = edit_message.await_args.kwargs
    # message_type drives editMessageCaption instead of editMessageText.
    assert kwargs["message_type"] == "image"
    assert kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "analyze:77"
    assert result["delivery_status"] == "delivered"


def test_format_node_keeps_a_mark_free_copy_for_in_place_edits():
    from processor.src.pipeline.nodes import format_node

    result = format_node(_base_state(is_edited=True))

    assert "✏️" in result["formatted_text"]
    assert "✏️" not in result["formatted_text_plain"]
    assert "Alice" in result["formatted_text_plain"]
