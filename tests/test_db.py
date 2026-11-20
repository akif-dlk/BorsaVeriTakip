from datetime import date

from bvt import db
from bvt.raw_loader import LoadResult


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self):
        self.calls, self.commits = [], 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Result((42,))

    def commit(self):
        self.commits += 1


def test_start_run_records_window():
    conn = _Conn()
    assert db.start_run(conn, "fx", date(2024, 1, 1), date(2024, 1, 31)) == 42
    sql, params = conn.calls[0]
    assert "window_start, window_end, run_trigger" in sql
    assert params == ("fx", date(2024, 1, 1), date(2024, 1, 31), "manual")
    assert conn.commits == 1


def test_start_run_records_trigger_from_context():
    from bvt.run_context import run_trigger

    conn = _Conn()
    with run_trigger("backfill"):
        db.start_run(conn, "stocks")
    assert conn.calls[0][1][-1] == "backfill"


def test_connect_retries_transient_errors(monkeypatch):
    import psycopg

    attempts = []

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_connect(dsn):
        attempts.append(dsn)
        if len(attempts) < 3:
            raise psycopg.OperationalError("connection refused")
        return _C()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr("bvt.retry.time.sleep", lambda s: None)
    with db.connect() as conn:
        assert isinstance(conn, _C)
    assert len(attempts) == 3


def test_finish_run_writes_load_breakdown():
    conn = _Conn()
    db.finish_run(conn, 42, "success", LoadResult(inserted=3, updated=1, unchanged=6))
    sql, params = conn.calls[0]
    assert "rows_inserted" in sql and "rows_unchanged" in sql
    assert params == ("success", 10, 3, 1, 6, None, 42)


def test_finish_run_failure_without_result():
    conn = _Conn()
    db.finish_run(conn, 7, "failed", error="boom")
    assert conn.calls[0][1] == ("failed", 0, 0, 0, 0, "boom", 7)


def test_init_scripts_are_ordered_and_rerunnable():
    names = [p.name for p in sorted(db.SQL_INIT_DIR.glob("*.sql"))]
    assert names[0] == "001_schemas.sql" and "005_raw_idempotency.sql" in names
    for path in db.SQL_INIT_DIR.glob("*.sql"):
        text = path.read_text(encoding="utf-8").upper()
        for stmt in ("CREATE TABLE ", "CREATE SCHEMA ", "CREATE INDEX ", "ADD COLUMN "):
            # Her oluşturma ifadesi IF NOT EXISTS içermeli (betikler tekrar çalıştırılabilir)
            assert text.count(stmt) == text.count(stmt + "IF NOT EXISTS"), (path.name, stmt)
