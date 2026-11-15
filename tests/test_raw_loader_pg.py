"""Gerçek PostgreSQL'e karşı idempotent yükleme testi.

Varsayılan olarak atlanır. Çalıştırmak için:
    docker compose up -d postgres
    BVT_TEST_PG=1 python -m pytest -q tests/test_raw_loader_pg.py

Tüm yazımlar tek transaction'da yapılır ve sonunda geri alınır; veritabanında iz bırakmaz.
"""
import os
from datetime import date

import pytest

from bvt import raw_loader
from bvt.raw_loader import FX_RATES, GOLD_PRICES, STOCK_PRICES

pytestmark = pytest.mark.skipif(not os.getenv("BVT_TEST_PG"), reason="BVT_TEST_PG ayarlı değil (PostgreSQL gerekir)")

D = date(1999, 1, 4)  # gerçek veriyle çakışmayacak bir tarih


@pytest.fixture
def conn():
    from bvt.db import connect, init_schema

    with connect() as c:
        init_schema(c)
        yield c
        c.rollback()


def _count(conn, table, where, params):
    return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]


def test_fx_same_data_twice_does_not_duplicate_or_rewrite(conn):
    rows = [("USDTRY", D, "test", 1.0, 1.2, 0.9, 1.1), ("EURTRY", D, "test", None, None, None, 1.3)]
    first = raw_loader.load_rows(conn, FX_RATES, rows, commit=False)
    stamp = conn.execute("SELECT fetched_at FROM raw.fx_rates WHERE pair='USDTRY' AND rate_date=%s AND "
                         "source='test'", (D,)).fetchone()[0]
    second = raw_loader.load_rows(conn, FX_RATES, rows, commit=False)
    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert _count(conn, "raw.fx_rates", "rate_date = %s AND source = 'test'", (D,)) == 2
    assert conn.execute("SELECT fetched_at FROM raw.fx_rates WHERE pair='USDTRY' AND rate_date=%s AND "
                        "source='test'", (D,)).fetchone()[0] == stamp


def test_fx_revised_value_is_updated(conn):
    raw_loader.load_rows(conn, FX_RATES, [("USDTRY", D, "test", None, None, None, 1.1)], commit=False)
    result = raw_loader.load_rows(conn, FX_RATES, [("USDTRY", D, "test", None, None, None, 1.15)], commit=False)
    assert (result.inserted, result.updated) == (0, 1)
    close = conn.execute("SELECT close FROM raw.fx_rates WHERE pair='USDTRY' AND rate_date=%s AND source='test'",
                         (D,)).fetchone()[0]
    assert float(close) == 1.15


def test_stock_and_gold_upsert_with_nulls(conn):
    stock = [("TEST", D, "test", 10.0, 11.0, 9.5, 10.5, 10.4, 1000), ("TEST", D, "test", 10.0, 11.0, 9.5, 10.5, 10.4,
                                                                      1000)]
    assert raw_loader.load_rows(conn, STOCK_PRICES, stock, commit=False).inserted == 1
    gold = [("XAUUSD_OZ", D, "test", "USD", None, None, None, 290.0, None)]
    assert raw_loader.load_rows(conn, GOLD_PRICES, gold, commit=False).inserted == 1
    assert raw_loader.load_rows(conn, GOLD_PRICES, gold, commit=False).unchanged == 1
