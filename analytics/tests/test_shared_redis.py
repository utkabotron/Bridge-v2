"""shared.redis_client: the one place the flows take their Redis connection settings from."""
from __future__ import annotations

import importlib

import pytest
import redis
from flows import shared


@pytest.fixture
def clients(monkeypatch):
    """Patch redis.Redis; returns the list of kwargs each client was built with."""
    calls: list[dict] = []

    class FakeRedis:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def delete(self, *keys):
            return len(keys)

    monkeypatch.setattr(redis, "Redis", FakeRedis)
    return calls


@pytest.fixture
def load_shared(monkeypatch):
    """Re-import shared under a patched environment; restore the real one afterwards."""
    def _load(**env):
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return importlib.reload(shared)

    yield _load
    monkeypatch.undo()
    importlib.reload(shared)


def test_password_and_address_come_from_env(load_shared, clients):
    mod = load_shared(REDIS_HOST="r", REDIS_PORT="6380", REDIS_DB="2", REDIS_PASSWORD="s3cret")
    mod.redis_client()
    assert clients == [{"host": "r", "port": 6380, "db": 2, "password": "s3cret", "socket_timeout": 3}]


@pytest.mark.parametrize("value", ["", None])
def test_empty_or_unset_password_means_no_auth(load_shared, clients, value):
    # A missing .env line must keep a passwordless Redis reachable, not send AUTH "".
    load_shared(REDIS_PASSWORD=value).redis_client()
    assert clients[0]["password"] is None


def test_keyword_args_override_defaults(clients):
    shared.redis_client(decode_responses=True, socket_timeout=5)
    assert clients[0]["socket_timeout"] == 5
    assert clients[0]["decode_responses"] is True


def test_flows_build_their_clients_through_the_factory(clients, monkeypatch):
    """The password reaches every flow's client, not just the factory's own callers."""
    from flows import health_check

    monkeypatch.setattr(shared, "REDIS_PASSWORD", "s3cret")
    health_check._redis()
    assert shared.invalidate_profile_cache([1, 2]) == 2
    assert [c["password"] for c in clients] == ["s3cret", "s3cret"]
    assert clients[0]["decode_responses"] is True
