"""PostgreSQL bağlantı ve şema yardımcıları."""
from __future__ import annotations

import argparse
import logging
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg

from bvt.config import pg_dsn
from bvt.raw_loader import LoadResult
from bvt.retry import retry_call
from bvt.run_context import current_trigger

log = logging.getLogger(__name__)

SQL_INIT_DIR = Path(__file__).resolve().parents[2] / "sql" / "init"


@contextmanager
def connect(attempts: int = 3):
    """Veri veritabanına bağlanır. Geçici bağlantı hatalarında (ör. konteyner yeni açılıyor) üstel bekleme
    ile yeniden dener; sorgu hataları yeniden denenmez."""
    conn = retry_call(psycopg.connect, pg_dsn(), attempts=attempts, base_delay=2.0, max_delay=15.0,
                      exceptions=(psycopg.OperationalError,))
    with conn:
        yield conn


def init_schema(conn: psycopg.Connection, sql_dir: Path = SQL_INIT_DIR) -> list[str]:
    """`sql/init/*.sql` betiklerini sırayla uygular.

    Betikler `IF NOT EXISTS` ile yazıldığından tekrar çalıştırmak güvenlidir. Docker yalnızca boş bir
    volume'de init betiklerini çalıştırdığı için, mevcut bir veritabanına yeni tablolar bu fonksiyonla eklenir.
    """
    applied = []
    for path in sorted(sql_dir.glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    conn.commit()
    return applied


def start_run(conn: psycopg.Connection, source: str, window_start: date | None = None,
              window_end: date | None = None) -> int:
    """ops.pipeline_runs'a 'running' kaydı açar ve run_id döner.

    Tetikleyici (manual / airflow / backfill / repair) `bvt.run_context`'ten alınır.
    """
    run_id = conn.execute(
        "INSERT INTO ops.pipeline_runs (source, window_start, window_end, run_trigger) VALUES (%s, %s, %s, %s) "
        "RETURNING run_id",
        (source, window_start, window_end, current_trigger()),
    ).fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, status: str, result: LoadResult | None = None,
               error: str | None = None) -> None:
    """Çalışmayı kapatır; `result` verilirse eklenen/güncellenen/değişmeyen satır sayılarını yazar."""
    r = result or LoadResult()
    conn.execute(
        "UPDATE ops.pipeline_runs SET finished_at = now(), status = %s, rows_loaded = %s, rows_inserted = %s, "
        "rows_updated = %s, rows_unchanged = %s, error = %s WHERE run_id = %s",
        (status, r.total, r.inserted, r.updated, r.unchanged, error, run_id),
    )
    conn.commit()


RAW_SUMMARY_SQL = """
SELECT 'raw.fx_rates' AS tbl, pair AS instrument, source, count(*), min(rate_date), max(rate_date)
  FROM raw.fx_rates GROUP BY pair, source
UNION ALL
SELECT 'raw.stock_prices', symbol, source, count(*), min(price_date), max(price_date)
  FROM raw.stock_prices GROUP BY symbol, source
UNION ALL
SELECT 'raw.gold_prices', instrument, source, count(*), min(price_date), max(price_date)
  FROM raw.gold_prices GROUP BY instrument, source
ORDER BY 1, 2, 3
"""


def raw_summary(conn: psycopg.Connection) -> list[tuple]:
    """Raw tablolarda enstrüman/kaynak başına satır sayısı ve tarih aralığı."""
    return conn.execute(RAW_SUMMARY_SQL).fetchall()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Veritabanı bağlantısını test eder / şemayı uygular.")
    parser.add_argument("--init", action="store_true", help="sql/init/*.sql betiklerini uygula")
    parser.add_argument("--summary", action="store_true", help="raw tabloların satır sayısı ve tarih aralığı")
    args = parser.parse_args()
    with connect() as conn:
        print(conn.execute("select version()").fetchone()[0])
        if args.init:
            print("Uygulandı:", ", ".join(init_schema(conn)))
        if args.summary:
            for tbl, inst, src, n, first, last in raw_summary(conn):
                print(f"{tbl:<18} {inst:<12} {src:<9} {n:>7} satır  {first} .. {last}")
