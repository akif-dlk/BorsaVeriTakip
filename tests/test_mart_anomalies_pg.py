"""mart_price_anomalies SQL'i ile bvt.anomalies'in gerçek PostgreSQL'de karşılaştırması + singular testler.

Varsayılan olarak atlanır. Çalıştırmak için:
    docker compose up -d postgres
    BVT_TEST_PG=1 python -m pytest -q tests/test_mart_anomalies_pg.py

dbt kurulumu gerekmez: model ve singular testler Jinja ile derlenir, `ref()` geçici tablolara yönlendirilir.
Her şey tek transaction'da yapılır ve geri alınır; veritabanında iz bırakmaz.
"""
import math
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bvt import anomalies as an

pytestmark = pytest.mark.skipif(not os.getenv("BVT_TEST_PG"), reason="BVT_TEST_PG ayarlı değil (PostgreSQL gerekir)")
jinja2 = pytest.importorskip("jinja2")

DBT = Path(__file__).resolve().parents[1] / "dbt"


def render(path: Path) -> str:
    template = jinja2.Environment().from_string(path.read_text(encoding="utf-8"))
    return template.render(ref=lambda name: f"pg_temp.{name}", config=lambda **_: "")


def synthetic_prices() -> pd.DataFrame:
    """Her kurala takılan ve takılmayan günler içeren dört enstrüman."""
    rows = []
    specs = [("USDTRY", "fx", 0.002), ("THYAO", "stock", 0.015), ("XU100", "index", 0.01), ("XAUUSD_OZ", "gold", 0.008)]
    for code, asset_class, amp in specs:
        day, level = date(2024, 1, 1), 1.0
        for i in range(160):
            while day.weekday() >= 5:
                day += timedelta(days=1)
            if code == "USDTRY" and i == 100:
                level *= 1.05          # sakin seride z-skoru sıçraması (eşik altı getiri)
            if code == "THYAO" and i == 120:
                level *= 0.6           # düzeltilmemiş bölünme benzeri -%40 → return_spike + zscore
            price = 100 * level * (1 + amp * math.sin(i * 1.3) + 0.0004 * i)
            if code == "XU100" and 60 <= i < 67:
                price = 9000.0         # 7 gün aynı fiyat → stale_price (5., 6., 7. gün)
            volume = (0 if i in (30, 31) else 1000 + i) if asset_class == "stock" else None
            valid = not (code == "XAUUSD_OZ" and i == 50)
            rows.append((code, asset_class, day, round(price, 6), volume, valid))
            day += timedelta(days=1)
    return pd.DataFrame(rows, columns=["instrument_code", "asset_class", "price_date", "adj_close", "volume",
                                       "has_valid_ohlc"])


@pytest.fixture
def conn():
    from bvt.db import connect

    with connect() as c:
        yield c
        c.rollback()


def _query(cur, sql: str) -> pd.DataFrame:
    cur.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def _load_fact(conn, prices: pd.DataFrame) -> None:
    conn.execute("""
        CREATE TEMP TABLE fact_daily_prices (
            instrument_key text, date_key integer, instrument_code text, price_date date, asset_class text,
            adj_close numeric(18, 6), volume bigint, has_valid_ohlc boolean
        ) ON COMMIT DROP""")
    with conn.cursor() as cur, cur.copy("COPY pg_temp.fact_daily_prices FROM STDIN") as copy:
        for r in prices.itertuples(index=False):
            copy.write_row((r.instrument_code.lower(), int(r.price_date.strftime("%Y%m%d")), r.instrument_code,
                            r.price_date, r.asset_class, r.adj_close,
                            None if pd.isna(r.volume) else int(r.volume), bool(r.has_valid_ohlc)))


def test_sql_matches_python_reference(conn):
    prices = synthetic_prices()
    _load_fact(conn, prices)
    with conn.cursor() as cur:
        got = _query(cur, render(DBT / "models" / "marts" / "mart_price_anomalies.sql"))
    want = an.detect_anomalies(prices)

    keys = ["instrument_code", "price_date", "anomaly_type"]
    assert sorted(map(tuple, got[keys].values.tolist())) == sorted(map(tuple, want[keys].values.tolist()))
    assert set(want.anomaly_type) == set(an.ANOMALY_TYPES)  # sentetik veri her kuralı tetikliyor
    m = got.merge(want, on=keys, suffixes=("_sql", "_py"))
    assert (m.severity_sql == m.severity_py).all()
    for col in ("metric_value", "threshold", "adj_close", "prev_adj_close"):
        a = pd.Series([math.nan if v is None else float(v) for v in m[f"{col}_sql"]])
        b = m[f"{col}_py"].astype(float).reset_index(drop=True)
        assert (a.isna() == b.isna()).all(), col
        assert a.dropna().tolist() == pytest.approx(b.dropna().tolist(), rel=1e-9, abs=1e-9), col


def test_gram_gold_singular_test(conn):
    conn.execute("""
        CREATE TEMP TABLE stg_gold_prices (
            instrument_code text, price_date date, close numeric(18, 6), usdtry numeric(18, 6),
            is_primary_source boolean
        ) ON COMMIT DROP""")
    ounce, usdtry = 2650.0, 34.5
    gram = round(ounce * usdtry / 31.1034768, 6)
    conn.execute("""
        INSERT INTO pg_temp.stg_gold_prices VALUES
            ('XAUUSD_OZ', '2026-11-02', %(ounce)s, NULL, true),
            ('XAUTRY_GRAM', '2026-11-02', %(gram)s, %(usdtry)s, true),          -- tutarlı
            ('XAUUSD_OZ', '2026-11-03', %(ounce)s, NULL, true),
            ('XAUTRY_GRAM', '2026-11-03', %(wrong)s, %(usdtry)s, true),         -- %%2 sapma
            ('XAUTRY_GRAM', '2026-11-04', %(gram)s, %(usdtry)s, true)           -- ons yok
    """, {"ounce": ounce, "gram": gram, "usdtry": usdtry, "wrong": gram * 1.02})
    with conn.cursor() as cur:
        failing = _query(cur, render(DBT / "tests" / "assert_gram_gold_matches_ounce.sql"))
    assert dict(zip(failing.price_date.astype(str), failing.problem)) == {"2026-11-03": "mismatch",
                                                                          "2026-11-04": "ounce_missing"}


def test_fact_coverage_and_recent_anomaly_tests(conn):
    conn.execute("""
        CREATE TEMP TABLE fact_daily_prices (instrument_key text, price_date date) ON COMMIT DROP;
        CREATE TEMP TABLE dim_instrument (
            instrument_key text, instrument_code text, n_price_days bigint
        ) ON COMMIT DROP;
        INSERT INTO pg_temp.fact_daily_prices VALUES ('a', '2026-11-02'), ('a', '2026-11-03'), ('b', '2026-11-02');
        INSERT INTO pg_temp.dim_instrument VALUES ('a', 'A', 2), ('b', 'B', 2), ('c', 'C', 0);
        CREATE TEMP TABLE mart_price_anomalies (
            instrument_code text, price_date date, anomaly_type text, severity text, metric_value double precision,
            threshold double precision, adj_close numeric, prev_adj_close double precision
        ) ON COMMIT DROP;
        INSERT INTO pg_temp.mart_price_anomalies VALUES
            ('A', current_date - 1, 'return_spike', 'error', 0.3, 0.25, 10, 7.7),
            ('A', current_date - 1, 'zscore_spike', 'warn', 6, 5, 10, 7.7),
            ('B', current_date - 400, 'return_spike', 'error', -0.3, 0.25, 7, 10);
    """)
    with conn.cursor() as cur:
        coverage = _query(cur, render(DBT / "tests" / "assert_fact_covers_primary_prices.sql"))
        recent = _query(cur, render(DBT / "tests" / "warn_recent_price_anomalies.sql"))
    assert coverage.instrument_code.tolist() == ["B"]  # 2 gün bekleniyor, 1 satır var
    assert recent[["instrument_code", "anomaly_type"]].values.tolist() == [["A", "return_spike"]]
