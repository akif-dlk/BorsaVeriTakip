"""PostgreSQL bağlantı ve şema yardımcıları."""
from __future__ import annotations

import argparse
import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg

from bvt.config import pg_dsn

log = logging.getLogger(__name__)

SQL_INIT_DIR = Path(__file__).resolve().parents[2] / "sql" / "init"


@contextmanager
def connect():
    with psycopg.connect(pg_dsn()) as conn:
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


def start_run(conn: psycopg.Connection, source: str) -> int:
    """ops.pipeline_runs'a 'running' kaydı açar ve run_id döner."""
    run_id = conn.execute(
        "INSERT INTO ops.pipeline_runs (source) VALUES (%s) RETURNING run_id", (source,)
    ).fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, status: str, rows_loaded: int | None = None,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE ops.pipeline_runs SET finished_at = now(), status = %s, rows_loaded = %s, error = %s "
        "WHERE run_id = %s",
        (status, rows_loaded, error, run_id),
    )
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Veritabanı bağlantısını test eder / şemayı uygular.")
    parser.add_argument("--init", action="store_true", help="sql/init/*.sql betiklerini uygula")
    args = parser.parse_args()
    with connect() as conn:
        print(conn.execute("select version()").fetchone()[0])
        if args.init:
            print("Uygulandı:", ", ".join(init_schema(conn)))
