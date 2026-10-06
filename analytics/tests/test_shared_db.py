"""shared.db_conn: the one place that opens, commits, rolls back and closes."""
from __future__ import annotations

import psycopg2
import psycopg2.extras
import pytest
from fake_db import FakeConn
from flows import shared


@pytest.fixture
def connects(monkeypatch):
    """Patch psycopg2.connect; returns the list of (args, kwargs, conn) it was called with."""
    calls: list[tuple] = []

    def fake_connect(*args, **kwargs):
        conn = FakeConn()
        calls.append((args, kwargs, conn))
        return conn

    monkeypatch.setattr(shared.psycopg2, "connect", fake_connect)
    return calls


def test_commits_then_closes_when_the_block_succeeds(connects):
    with shared.db_conn() as conn:
        assert conn.events == []  # nothing is committed while the block is still running

    assert connects[0][2].events == ["commit", "close"]


def test_rolls_back_and_closes_when_the_block_raises(connects):
    with pytest.raises(ValueError, match="boom"), shared.db_conn():
        raise ValueError("boom")

    assert connects[0][2].events == ["rollback", "close"]  # never a commit


def test_a_failing_commit_rolls_back_and_still_closes(connects, monkeypatch):
    def broken_commit(self=None):
        raise psycopg2.OperationalError("server closed the connection")

    with pytest.raises(psycopg2.OperationalError), shared.db_conn() as conn:
        monkeypatch.setattr(conn, "commit", broken_commit)

    assert connects[0][2].events == ["rollback", "close"]


def test_a_dead_connection_does_not_mask_the_original_error(connects):
    def dead_rollback():
        raise psycopg2.InterfaceError("connection already closed")

    with pytest.raises(ValueError, match="the real problem"), shared.db_conn() as conn:
        conn.rollback = dead_rollback
        raise ValueError("the real problem")

    assert connects[0][2].events == ["close"]


def test_dict_rows_by_default_and_tuples_on_request(connects):
    with shared.db_conn():
        pass
    with shared.db_conn(cursor_factory=None):
        pass

    assert connects[0][0] == (shared.DB_URL,)
    assert connects[0][1]["cursor_factory"] is psycopg2.extras.RealDictCursor
    assert connects[1][1]["cursor_factory"] is None
