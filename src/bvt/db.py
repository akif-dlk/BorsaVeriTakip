"""PostgreSQL bağlantı yardımcısı."""
from contextlib import contextmanager

import psycopg

from bvt.config import pg_dsn


@contextmanager
def connect():
    with psycopg.connect(pg_dsn()) as conn:
        yield conn


if __name__ == "__main__":
    with connect() as conn:
        print(conn.execute("select version()").fetchone()[0])
