"""A recording stand-in for a psycopg2 connection, for tests that must not touch a database."""
from __future__ import annotations

from contextlib import contextmanager


class FakeCursor:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.statements.append(("execute", " ".join(sql.split()), params))

    def executemany(self, sql, rows):
        self.conn.statements.append(("executemany", " ".join(sql.split()), list(rows)))

    def fetchone(self):
        return self.conn.fetchone_results.pop(0) if self.conn.fetchone_results else None

    def fetchall(self):
        return self.conn.fetchall_results.pop(0) if self.conn.fetchall_results else []


class FakeConn:
    """Records statements and lifecycle events; answers fetches from queues the test fills."""

    def __init__(self, fetchone=(), fetchall=()):
        self.statements: list[tuple] = []
        self.events: list[str] = []
        self.fetchone_results = list(fetchone)
        self.fetchall_results = list(fetchall)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")

    def executed(self, kind: str) -> list[tuple]:
        return [s for s in self.statements if s[0] == kind]


def patch_db_conn(monkeypatch, module, conn: FakeConn | None = None) -> list[FakeConn]:
    """Replace `module.db_conn` so every `with db_conn()` yields a FakeConn; returns the ones opened."""
    opened: list[FakeConn] = []

    @contextmanager
    def fake_db_conn(cursor_factory=None):
        c = conn if conn is not None else FakeConn()
        opened.append(c)
        yield c

    monkeypatch.setattr(module, "db_conn", fake_db_conn)
    return opened
