"""Ortak test yardımcıları: psycopg bağlantısını taklit eden sahte nesneler."""
import pytest


class FakeCopy:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self.conn.copied.append(tuple(row))


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)

    def copy(self, sql):
        self.conn.copy_sql.append(sql)
        return FakeCopy(self.conn)

    def fetchall(self):
        # RETURNING (xmax = 0): her kopyalanan satır "yeni" kabul edilir, ya da testin verdiği bayraklar
        flags = self.conn.returning if self.conn.returning is not None else [True] * len(self.conn.copied)
        return [(f,) for f in flags]


class FakeConn:
    def __init__(self, returning=None):
        self.executed, self.copy_sql, self.copied = [], [], []
        self.commits = 0
        self.returning = returning

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


@pytest.fixture
def fake_conn():
    return FakeConn()
