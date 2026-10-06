"""config.redis_kwargs: the one place every processor Redis client takes its options from."""
from __future__ import annotations

import importlib

import pytest

from processor.src import config


@pytest.fixture
def load_config(monkeypatch):
    """Re-import config under a patched environment; restore the real one afterwards."""
    def _load(**env):
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return importlib.reload(config)

    yield _load
    monkeypatch.undo()
    importlib.reload(config)


def test_password_is_passed_when_set(load_config):
    cfg = load_config(REDIS_PASSWORD="s3cret")
    assert cfg.redis_kwargs()["password"] == "s3cret"


@pytest.mark.parametrize("value", ["", None])
def test_empty_or_unset_password_means_no_auth(load_config, value):
    # An empty .env line must not turn into AUTH "" — a Redis without requirepass rejects it.
    cfg = load_config(REDIS_PASSWORD=value)
    assert cfg.redis_kwargs()["password"] is None


def test_overrides_still_win(load_config):
    cfg = load_config(REDIS_PASSWORD="s3cret")
    kwargs = cfg.redis_kwargs(decode_responses=False)
    assert kwargs["decode_responses"] is False
    assert kwargs["password"] == "s3cret"
