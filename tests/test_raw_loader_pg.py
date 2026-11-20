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


def test_gap_detection_and_snapshot(conn):
    from datetime import timedelta

    from bvt import gaps

    # 1999-01-04 Pazartesi. TEST_A her gün var; TEST_B Çarşamba yok; Perşembe ikisi de yok (tatil).
    days = [D + timedelta(days=i) for i in range(5)]
    rows = [("TEST_A", d, "yfinance", 1, 1, 1, 1, 1, 1) for d in days if d != days[3]]
    rows += [("TEST_B", d, "yfinance", 1, 1, 1, 1, 1, 1) for d in days if d not in (days[2], days[3])]
    raw_loader.load_rows(conn, STOCK_PRICES, rows, commit=False)
    observed, first = gaps.observed_dates(conn, "stocks", days[0], days[4])
    found = gaps.find_gaps("stocks", observed, first, days[0], days[4], ["TEST_A", "TEST_B"])
    assert [(g.gap_date, g.instrument, g.kind) for g in found] == [
        (days[2], "TEST_B", gaps.MISSING), (days[3], "*", gaps.MARKET_CLOSED)]
    conn.commit = lambda: None  # save_gaps commit eder; testte geri alınabilir kalsın
    gaps.save_gaps(conn, "stocks", days[0], days[4], found)
    gaps.save_gaps(conn, "stocks", days[0], days[4], found)  # anlık görüntü: tekrar yazım kopya üretmez
    assert _count(conn, "ops.data_gaps", "source = 'stocks' AND gap_date BETWEEN %s AND %s", (days[0], days[4])) == 2
