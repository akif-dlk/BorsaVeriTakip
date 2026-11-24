"""marts.mart_indicators SQL'i ile Python başvuru uygulamasının (bvt.indicators) gerçek PostgreSQL'de karşılaştırması.

Varsayılan olarak atlanır. Çalıştırmak için:
    docker compose up -d postgres
    BVT_TEST_PG=1 python -m pytest -q tests/test_mart_indicators_pg.py

dbt kurulumu gerekmez: model Jinja ile derlenir, `ref('fact_daily_prices')` geçici bir tabloya yönlendirilir.
Her şey tek transaction'da yapılır ve geri alınır; veritabanında iz bırakmaz.
"""
import math
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bvt import indicators as ind

pytestmark = pytest.mark.skipif(not os.getenv("BVT_TEST_PG"), reason="BVT_TEST_PG ayarlı değil (PostgreSQL gerekir)")
jinja2 = pytest.importorskip("jinja2")

MODEL = Path(__file__).resolve().parents[1] / "dbt" / "models" / "marts" / "mart_indicators.sql"


def render_model() -> str:
    template = jinja2.Environment().from_string(MODEL.read_text(encoding="utf-8"))
    return template.render(ref=lambda name: f"pg_temp.{name}")


def synthetic_prices() -> pd.DataFrame:
    rows = []
    for code, n, base in [("THYAO", 260, 250.0), ("USDTRY", 40, 34.0), ("XU100", 5, 9500.0)]:
        day = date(2024, 1, 1)
        for i in range(n):
            while day.weekday() >= 5:  # hafta sonu boşlukları: göstergeler işlem günü üzerinde sayılır
                day += timedelta(days=1)
            price = base * (1 + 0.05 * math.sin(i / 4) + 0.001 * i)
            rows.append((code, day, round(price, 6)))
            day += timedelta(days=1)
    return pd.DataFrame(rows, columns=["instrument_code", "price_date", "adj_close"])


@pytest.fixture
def conn():
    from bvt.db import connect

    with connect() as c:
        yield c
        c.rollback()


def test_sql_matches_python_reference(conn):
    prices = synthetic_prices()
    conn.execute("""
        CREATE TEMP TABLE fact_daily_prices (
            instrument_key text, date_key integer, instrument_code text, price_date date,
            asset_class text, adj_close numeric(18, 6)
        ) ON COMMIT DROP""")
    with conn.cursor() as cur:
        with cur.copy("COPY pg_temp.fact_daily_prices FROM STDIN") as copy:
            for r in prices.itertuples(index=False):
                copy.write_row((r.instrument_code.lower(), int(r.price_date.strftime("%Y%m%d")), r.instrument_code,
                                r.price_date, "stock", r.adj_close))
        cur.execute(render_model())
        cols = [d.name for d in cur.description]
        sql = pd.DataFrame(cur.fetchall(), columns=cols)

    assert len(sql) == len(prices)
    assert list(cols[cols.index("log_return"):]) == [*ind.INDICATOR_COLUMNS[:-1], "rsi_zone", "volatility_20d"]
    expected = ind.compute_indicators(prices)
    merged = sql.merge(expected, on=["instrument_code", "price_date"], suffixes=("_sql", "_py"))
    assert len(merged) == len(prices)
    assert (merged.obs_index_sql == merged.obs_index_py).all()
    assert (merged.rsi_zone_sql.fillna("-") == merged.rsi_zone_py.fillna("-")).all()
    for col in ind.INDICATOR_COLUMNS:
        got = pd.to_numeric(merged[f"{col}_sql"], errors="coerce").astype("float64")
        want = merged[f"{col}_py"].astype("float64")
        assert (got.isna() == want.isna()).all(), col
        assert got.dropna().tolist() == pytest.approx(want.dropna().tolist(), rel=1e-9, abs=1e-9), col
    short = sql[sql.instrument_code == "XU100"]
    assert short[list(ind.INDICATOR_COLUMNS[1:])].isna().all().all()  # 5 gün: hiçbir pencere dolmaz
