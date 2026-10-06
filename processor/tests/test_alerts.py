"""Admin alerting: the shared notifier, the sliding-window latch and the trackers built on them."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def clock(monkeypatch):
    """Controllable monotonic time for SlidingWindow."""
    from processor.src import alerts

    now = [1000.0]
    monkeypatch.setattr(alerts, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


# ── SlidingWindow ─────────────────────────────────────────

def test_window_forgets_events_older_than_its_span(clock):
    from processor.src.alerts import SlidingWindow

    w = SlidingWindow(900)
    w.record(hit=True)
    clock[0] += 600
    w.record(hit=False)
    assert (w.total, w.hits) == (2, 1)

    clock[0] += 400  # the first event is now 1000s old
    w.record(hit=False)
    assert (w.total, w.hits) == (2, 0)


def test_window_alerts_once_until_rearmed():
    from processor.src.alerts import SlidingWindow

    w = SlidingWindow(900)
    assert w.fire() is True
    assert w.fire() is False  # same incident
    w.rearm()
    assert w.fire() is True


def test_window_cooldown_rearms_by_itself_and_rearm_is_not_needed(clock):
    """Translation failures: a quiet window does not re-arm; the cooldown does."""
    from processor.src.alerts import SlidingWindow

    w = SlidingWindow(900, cooldown=3600)
    w.record()
    assert w.fire() is True

    clock[0] += 1800  # window long drained, cooldown not over
    w.record()
    assert w.fire() is False

    clock[0] += 1801
    w.record()
    assert w.fire() is True


# ── notify_admins ─────────────────────────────────────────

@pytest.fixture
def telegram():
    from processor.src import alerts
    import processor.src.telegram_sender as tg

    client = MagicMock()
    client.post = AsyncMock(return_value=SimpleNamespace(status_code=200, text="{}"))
    with patch.object(alerts, "is_enabled", new=AsyncMock(return_value=True)), \
         patch.object(alerts, "ADMIN_TG_IDS", [1, 2]), \
         patch.object(tg, "BOT_TOKEN", "TOKEN"), \
         patch.object(tg, "get_client", return_value=client):
        yield alerts, client


@pytest.mark.asyncio
async def test_notify_admins_sends_html_to_every_admin_through_the_shared_client(telegram):
    alerts, client = telegram

    assert await alerts.notify_admins("<b>hi</b>") == 2

    sent = [(c.args[0], c.kwargs["json"]) for c in client.post.await_args_list]
    assert [body["chat_id"] for _, body in sent] == [1, 2]
    assert all(url.endswith("/sendMessage") for url, _ in sent)
    assert all(body["text"] == "<b>hi</b>" and body["parse_mode"] == "HTML" for _, body in sent)
    assert all(body["disable_web_page_preview"] is True for _, body in sent)


@pytest.mark.asyncio
async def test_notify_admins_is_silent_when_alerts_are_off(telegram):
    alerts, client = telegram

    with patch.object(alerts, "is_enabled", new=AsyncMock(return_value=False)):
        assert await alerts.notify_admins("x") == 0

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_admins_needs_a_token_and_an_admin(telegram):
    import processor.src.telegram_sender as tg
    alerts, client = telegram

    with patch.object(tg, "BOT_TOKEN", ""):
        assert await alerts.notify_admins("x") == 0
    with patch.object(alerts, "ADMIN_TG_IDS", []):
        assert await alerts.notify_admins("x") == 0

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_admins_survives_one_admin_failing(telegram):
    alerts, client = telegram
    client.post = AsyncMock(side_effect=[RuntimeError("boom"), SimpleNamespace(status_code=200, text="{}")])

    assert await alerts.notify_admins("x") == 1  # the second admin still got it


@pytest.mark.asyncio
async def test_notify_admins_does_not_count_a_rejected_send(telegram):
    alerts, client = telegram
    client.post = AsyncMock(return_value=SimpleNamespace(status_code=403, text="blocked"))

    assert await alerts.notify_admins("x") == 0


# ── trackers ──────────────────────────────────────────────

@pytest.fixture
def main_alerts(monkeypatch):
    """main's trackers with fresh windows and the sends mocked out."""
    import processor.src.main as main

    monkeypatch.setattr(main, "_unauth_window", main.SlidingWindow(main.UNAUTH_WINDOW))
    monkeypatch.setattr(main, "_delivery_window", main.SlidingWindow(main.FAILURE_RATE_WINDOW))
    unauth = AsyncMock()
    rate = AsyncMock()
    monkeypatch.setattr(main, "_alert_admins_unauthorized", unauth)
    monkeypatch.setattr(main, "_alert_admins_failure_rate", rate)
    return main, unauth, rate


@pytest.mark.asyncio
async def test_401_alert_fires_at_the_threshold_once(main_alerts):
    main, unauth, _ = main_alerts

    for _ in range(main.UNAUTH_THRESHOLD - 1):
        main._track_unauth_error()
    await asyncio.sleep(0)
    unauth.assert_not_awaited()

    for _ in range(3):  # the threshold, then more of the same incident
        main._track_unauth_error()
    await asyncio.sleep(0)
    unauth.assert_awaited_once()


@pytest.mark.asyncio
async def test_401_alert_fires_again_after_a_quiet_window(main_alerts, clock):
    """The old flag could never reset (its reset check ran right after an append): one alert per process."""
    main, unauth, _ = main_alerts

    for _ in range(main.UNAUTH_THRESHOLD):
        main._track_unauth_error()
    await asyncio.sleep(0)
    unauth.assert_awaited_once()

    clock[0] += main.UNAUTH_WINDOW + 1
    for _ in range(main.UNAUTH_THRESHOLD):
        main._track_unauth_error()
    await asyncio.sleep(0)
    assert unauth.await_count == 2


@pytest.mark.asyncio
async def test_failure_rate_waits_for_the_minimum_sample(main_alerts):
    main, _, rate = main_alerts

    for _ in range(main.FAILURE_RATE_MIN_MSGS - 1):
        main._track_delivery(failed=True)
    await asyncio.sleep(0)
    rate.assert_not_awaited()  # 100% failed, but too few messages to say

    main._track_delivery(failed=True)
    await asyncio.sleep(0)
    rate.assert_awaited_once_with(1.0, main.FAILURE_RATE_MIN_MSGS, main.FAILURE_RATE_MIN_MSGS)


@pytest.mark.asyncio
async def test_failure_rate_below_the_threshold_stays_quiet(main_alerts):
    main, _, rate = main_alerts

    for _ in range(200):  # 0.5% failed, threshold is 5%
        main._track_delivery(failed=False)
    main._track_delivery(failed=True)
    await asyncio.sleep(0)

    rate.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_rate_alerts_once_while_the_incident_lasts(main_alerts):
    main, _, rate = main_alerts

    for _ in range(main.FAILURE_RATE_MIN_MSGS):
        main._track_delivery(failed=True)
    for _ in range(10):
        main._track_delivery(failed=True)
        main._track_delivery(failed=False)
    await asyncio.sleep(0)

    rate.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_rate_alerts_again_once_a_failure_free_window_has_passed(main_alerts, clock):
    main, _, rate = main_alerts

    for _ in range(main.FAILURE_RATE_MIN_MSGS):
        main._track_delivery(failed=True)
    await asyncio.sleep(0)
    rate.assert_awaited_once()

    clock[0] += main.FAILURE_RATE_WINDOW + 1  # the failures age out
    for _ in range(main.FAILURE_RATE_MIN_MSGS):
        main._track_delivery(failed=False)  # a full, clean window re-arms the alert
    for _ in range(main.FAILURE_RATE_MIN_MSGS):
        main._track_delivery(failed=True)
    await asyncio.sleep(0)

    assert rate.await_count == 2


# ── alert texts and the DLQ cooldown ──────────────────────

@pytest.mark.asyncio
async def test_alert_functions_compose_text_and_hand_it_to_notify_admins():
    import processor.src.main as main

    with patch.object(main, "notify_admins", new=AsyncMock(return_value=2)) as notify:
        await main._alert_admins_unauthorized()
        await main._alert_admins_failure_rate(0.123, 3, 20)
        await main._alert_admins_translation(4, "Request timed out.")
        await main._alert_admins_translation(4, "Error code: 429 insufficient_quota")

    unauth, rate, failing, quota = [c.args[0] for c in notify.await_args_list]
    assert "401 Unauthorized spike detected" in unauth
    assert "12.3%" in rate and "3/20" in rate
    assert "Translation is failing" in failing and "<code>Request timed out.</code>" in failing
    assert "OpenAI credits ran out" in quota and main.OPENAI_BILLING_URL in quota


@pytest.mark.asyncio
async def test_dlq_alert_repeats_only_after_the_cooldown(monkeypatch):
    import processor.src.main as main

    monkeypatch.setattr(main, "_last_dlq_alert", 0.0)
    with patch.object(main, "notify_admins", new=AsyncMock(return_value=2)) as notify:
        await main._alert_admins_dlq(30)
        await main._alert_admins_dlq(31)

    notify.assert_awaited_once()
    assert "<b>30</b>" in notify.await_args.args[0]


@pytest.mark.asyncio
async def test_dlq_alert_is_retried_when_nothing_was_delivered(monkeypatch):
    """Alerts off, or Telegram unreachable: do not burn the whole cooldown on a silent pass."""
    import processor.src.main as main

    monkeypatch.setattr(main, "_last_dlq_alert", 0.0)
    with patch.object(main, "notify_admins", new=AsyncMock(return_value=0)) as notify:
        await main._alert_admins_dlq(30)
        await main._alert_admins_dlq(31)

    assert notify.await_count == 2


# ── startup token check ───────────────────────────────────

@pytest.mark.asyncio
async def test_bot_token_check_uses_the_shared_client():
    import processor.src.main as main
    import processor.src.telegram_sender as tg

    client = MagicMock()
    client.get = AsyncMock(return_value=SimpleNamespace(
        status_code=200, text="", json=lambda: {"result": {"username": "bridge_bot", "id": 7}},
    ))
    with patch.object(tg, "BOT_TOKEN", "TOKEN"), patch.object(tg, "get_client", return_value=client), \
         patch("httpx.AsyncClient", side_effect=AssertionError("no private client")):
        await main._validate_bot_token()

    assert client.get.await_args.args[0].endswith("/getMe")
