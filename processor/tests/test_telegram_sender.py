"""Unit tests for telegram_sender module."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Pure parser functions ──────────────────────────────────

def test_parse_migrate_found():
    from processor.src.telegram_sender import _parse_migrate
    resp = json.dumps({"parameters": {"migrate_to_chat_id": -1001234567890}})
    assert _parse_migrate(resp) == -1001234567890


def test_parse_migrate_not_found():
    from processor.src.telegram_sender import _parse_migrate
    assert _parse_migrate('{"ok": false}') is None
    assert _parse_migrate("not json") is None


def test_parse_retry_after_429():
    from processor.src.telegram_sender import _parse_retry_after
    resp = json.dumps({"error_code": 429, "parameters": {"retry_after": 30}})
    assert _parse_retry_after(resp) == 30


def test_parse_retry_after_not_429():
    from processor.src.telegram_sender import _parse_retry_after
    assert _parse_retry_after(json.dumps({"error_code": 400})) is None
    assert _parse_retry_after("bad json") is None


def test_is_unauthorized_true():
    from processor.src.telegram_sender import _is_unauthorized
    resp = json.dumps({"ok": False, "error_code": 401})
    assert _is_unauthorized(resp) is True


def test_is_unauthorized_false():
    from processor.src.telegram_sender import _is_unauthorized
    assert _is_unauthorized(json.dumps({"error_code": 400})) is False
    assert _is_unauthorized("not json") is False


def test_parse_message_id_ok():
    from processor.src.telegram_sender import _parse_message_id
    resp = json.dumps({"ok": True, "result": {"message_id": 42}})
    assert _parse_message_id(resp) == 42


def test_parse_message_id_error():
    from processor.src.telegram_sender import _parse_message_id
    assert _parse_message_id(json.dumps({"ok": False})) is None
    assert _parse_message_id("bad json") is None


def test_to_internal_url():
    from processor.src.telegram_sender import _to_internal_url
    url = "http://83.217.222.126:9000/bridge-media/test.jpg"
    result = _to_internal_url(url)
    assert "minio:9000" in result
    assert "bridge-media/test.jpg" in result


def test_filename_from_url_with_explicit_filename():
    from processor.src.telegram_sender import _filename_from_url
    assert _filename_from_url("http://host/path/file.jpg", "custom.jpg") == "custom.jpg"


def test_filename_from_url_from_path():
    from processor.src.telegram_sender import _filename_from_url
    assert _filename_from_url("http://host/path/image.png") == "image.png"


def test_filename_from_url_empty_path():
    from processor.src.telegram_sender import _filename_from_url
    assert _filename_from_url("http://host/") == "file"


# ── _send_text ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_text_success():
    from processor.src.telegram_sender import _send_text
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = json.dumps({"ok": True, "result": {"message_id": 99}})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        ok, err, msg_id = await _send_text(12345, "Hello")

    assert ok is True
    assert err is None
    assert msg_id == 99


@pytest.mark.asyncio
async def test_send_text_401():
    from processor.src.telegram_sender import _send_text
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = json.dumps({"ok": False, "error_code": 401})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        ok, err, msg_id = await _send_text(12345, "Hello")

    assert ok is False
    assert err == "401_UNAUTHORIZED"
    assert msg_id is None


@pytest.mark.asyncio
async def test_send_text_401_in_body():
    """401 detected via error_code in response body even when status != 401."""
    from processor.src.telegram_sender import _send_text
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = json.dumps({"ok": False, "error_code": 401})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        ok, err, msg_id = await _send_text(12345, "Hello")

    assert ok is False
    assert err == "401_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_send_text_html_parse_fallback():
    """400 'can't parse entities' → retry without parse_mode and succeed."""
    from processor.src.telegram_sender import _send_text

    resp_400 = MagicMock()
    resp_400.status_code = 400
    resp_400.text = "Bad Request: can't parse entities"

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.text = json.dumps({"ok": True, "result": {"message_id": 55}})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[resp_400, resp_200])
        mock_get_client.return_value = mock_client

        ok, err, msg_id = await _send_text(12345, "Hello <bad>")

    assert ok is True
    assert msg_id == 55


@pytest.mark.asyncio
async def test_send_text_generic_error():
    from processor.src.telegram_sender import _send_text
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        ok, err, msg_id = await _send_text(12345, "Hello")

    assert ok is False
    assert err == "Internal Server Error"
    assert msg_id is None


# ── send_message ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_success():
    from processor.src.telegram_sender import send_message
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = json.dumps({"ok": True, "result": {"message_id": 77}})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        ok, err, migrate_id, msg_id = await send_message(12345, "Hello")

    assert ok is True
    assert msg_id == 77
    assert migrate_id is None


@pytest.mark.asyncio
async def test_send_message_429_retries():
    """429 → wait and retry once."""
    from processor.src.telegram_sender import send_message

    resp_429 = MagicMock()
    resp_429.status_code = 400
    resp_429.text = json.dumps({"error_code": 429, "parameters": {"retry_after": 1}})

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.text = json.dumps({"ok": True, "result": {"message_id": 88}})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client, \
         patch("processor.src.telegram_sender.asyncio.sleep", new=AsyncMock()):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[resp_429, resp_200])
        mock_get_client.return_value = mock_client

        ok, err, migrate_id, msg_id = await send_message(12345, "Hello")

    assert ok is True
    assert msg_id == 88


@pytest.mark.asyncio
async def test_send_message_migrate_returned():
    """migrate_to_chat_id extracted from error response and returned."""
    from processor.src.telegram_sender import send_message

    migrate_chat_id = -1001234567890
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = json.dumps({"ok": False, "parameters": {"migrate_to_chat_id": migrate_chat_id}})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_client

        ok, err, migrate_id, msg_id = await send_message(12345, "Hello")

    assert migrate_id == migrate_chat_id


@pytest.mark.asyncio
async def test_send_message_exception_handled():
    """Network exception → returns failure tuple without raising."""
    from processor.src.telegram_sender import send_message

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("network error"))
        mock_get_client.return_value = mock_client

        ok, err, migrate_id, msg_id = await send_message(12345, "Hello")

    assert ok is False
    assert "network error" in err


# ── Length-limit splitting (4096 text / 1024 caption) ─────

def test_split_text_under_limit_single_chunk():
    from processor.src.telegram_sender import _split_text
    assert _split_text("hello", 4096) == ["hello"]


def test_split_text_prefers_newline_boundaries():
    from processor.src.telegram_sender import _split_text
    text = "\n".join(["line" + str(i) for i in range(1000)])
    chunks = _split_text(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    # Reassembling with newlines reproduces the original (we split on newlines).
    assert "\n".join(chunks) == text


def test_split_text_hard_splits_when_no_newline():
    from processor.src.telegram_sender import _split_text
    text = "x" * 5000
    chunks = _split_text(text, 4096)
    assert [len(c) for c in chunks] == [4096, 904]
    assert "".join(chunks) == text


def test_split_caption_under_limit():
    from processor.src.telegram_sender import _split_caption
    cap, overflow = _split_caption("short caption")
    assert cap == "short caption"
    assert overflow is None


def test_split_caption_overflow_kept():
    from processor.src.telegram_sender import _split_caption, TG_MAX_CAPTION
    cap, overflow = _split_caption("a" * 2000)
    assert len(cap) <= TG_MAX_CAPTION
    assert overflow is not None
    # Nothing is dropped: caption + overflow reconstruct the source.
    assert cap + overflow == "a" * 2000


# ── dead chat detection ───────────────────────────────────

def test_is_dead_chat_detects_unrecoverable_errors():
    """403/400 responses that mean the chat can never accept messages again."""
    from processor.src.telegram_sender import is_dead_chat

    assert is_dead_chat('{"ok":false,"error_code":403,"description":"Forbidden: the group chat was deleted"}')
    assert is_dead_chat('{"ok":false,"error_code":403,"description":"Forbidden: bot was kicked from the group chat"}')
    assert is_dead_chat('{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}')


def test_is_dead_chat_ignores_recoverable_errors():
    """A supergroup migration is recoverable — pausing the pair there would break the bridge."""
    from processor.src.telegram_sender import is_dead_chat

    migrated = ('{"ok":false,"error_code":400,"description":"Bad Request: group chat was upgraded to a '
                'supergroup chat","parameters":{"migrate_to_chat_id":-1004396105698}}')
    assert not is_dead_chat(migrated)
    assert not is_dead_chat('{"ok":false,"error_code":429,"description":"Too Many Requests: retry after 5"}')
    assert not is_dead_chat(None)
    assert not is_dead_chat("")


# ── Transient Telegram failures ───────────────────────────

def test_server_errors_are_recognised_as_transient():
    from processor.src.telegram_sender import _is_server_error

    assert _is_server_error('{"ok":false,"error_code":502,"description":"Bad Gateway"}')
    assert _is_server_error('{"ok":false,"error_code":500,"description":"Internal Server Error"}')
    assert _is_server_error("Connection reset by peer")
    assert _is_server_error("read timeout")


def test_client_errors_are_not_retried():
    """A 400/403 will fail identically on retry; only 5xx and transport errors are worth it."""
    from processor.src.telegram_sender import _is_server_error

    assert not _is_server_error('{"ok":false,"error_code":403,"description":"Forbidden"}')
    assert not _is_server_error('{"ok":false,"error_code":400,"description":"chat not found"}')
    assert not _is_server_error(None)


# ── Stage 3: voice notes and replies ──────────────────────

def test_whatsapp_voice_notes_map_to_sendVoice():
    """WhatsApp emits type "ptt"; it was absent from the map, so the media was dropped
    and the recipient got a message containing only the sender's name."""
    from processor.src.telegram_sender import _MEDIA_TYPE_MAP

    assert _MEDIA_TYPE_MAP["ptt"] == ("sendVoice", "voice")
    # sendAudio would render a file player rather than a voice bubble.
    assert _MEDIA_TYPE_MAP["voice"] == ("sendVoice", "voice")
    assert _MEDIA_TYPE_MAP["audio"] == ("sendAudio", "audio")


def test_reply_params_tolerate_a_deleted_target():
    """Delivery must not fail because the quoted message is gone from Telegram."""
    from processor.src.telegram_sender import _reply_params

    assert _reply_params(None) == {}
    params = _reply_params(42)["reply_parameters"]
    assert params["message_id"] == 42
    assert params["allow_sending_without_reply"] is True


# ── edit_message ───────────────────────────────────────────

def _resp(status_code: int, text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


@pytest.mark.asyncio
async def test_edit_message_text_uses_edit_message_text():
    from processor.src.telegram_sender import edit_message

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_resp(200, json.dumps({"ok": True})))
        mock_get_client.return_value = mock_client

        ok, err = await edit_message(12345, 42, "<b>Alice</b>\n\nновый текст")

    url, kwargs = mock_client.post.await_args.args[0], mock_client.post.await_args.kwargs
    assert url.endswith("/editMessageText")
    assert kwargs["json"]["text"] == "<b>Alice</b>\n\nновый текст"
    assert kwargs["json"]["message_id"] == 42
    assert ok is True and err is None


@pytest.mark.asyncio
async def test_edit_message_media_edits_the_caption():
    from processor.src.telegram_sender import edit_message

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_resp(200, json.dumps({"ok": True})))
        mock_get_client.return_value = mock_client

        ok, _ = await edit_message(12345, 42, "подпись", message_type="image")

    url, kwargs = mock_client.post.await_args.args[0], mock_client.post.await_args.kwargs
    assert url.endswith("/editMessageCaption")
    assert kwargs["json"]["caption"] == "подпись"
    assert "text" not in kwargs["json"]
    assert ok is True


@pytest.mark.asyncio
async def test_edit_message_refuses_text_that_was_split_on_delivery():
    """Over the limit the original went out in pieces, so no single message holds it."""
    from processor.src.telegram_sender import edit_message, TG_MAX_TEXT

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock()
        mock_get_client.return_value = mock_client

        ok, err = await edit_message(12345, 42, "x" * (TG_MAX_TEXT + 1))

    mock_client.post.assert_not_called()
    assert ok is False
    assert err == "edit_text_too_long"


@pytest.mark.asyncio
async def test_edit_message_treats_an_unchanged_message_as_done():
    """Resending here would recreate the duplicate that editing exists to remove."""
    from processor.src.telegram_sender import edit_message

    not_modified = json.dumps({
        "ok": False, "error_code": 400,
        "description": "Bad Request: message is not modified",
    })

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_resp(400, not_modified))
        mock_get_client.return_value = mock_client

        ok, err = await edit_message(12345, 42, "тот же текст")

    assert ok is True and err is None


@pytest.mark.asyncio
async def test_edit_message_retries_without_parse_mode_on_bad_html():
    from processor.src.telegram_sender import edit_message

    bad_html = json.dumps({"ok": False, "error_code": 400,
                           "description": "Bad Request: can't parse entities"})

    with patch("processor.src.telegram_sender.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[
            _resp(400, bad_html), _resp(200, json.dumps({"ok": True})),
        ])
        mock_get_client.return_value = mock_client

        ok, err = await edit_message(12345, 42, "a < b")

    assert ok is True and err is None
    assert "parse_mode" not in mock_client.post.await_args.kwargs["json"]
