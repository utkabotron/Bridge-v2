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
