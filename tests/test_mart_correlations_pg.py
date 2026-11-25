"""mart_returns, mart_period_returns, mart_correlations SQL'i ile bvt.returns'ün gerçek PostgreSQL'de karşılaştırması.

Varsayılan olarak atlanır. Çalıştırmak için:
    docker compose up -d postgres
    BVT_TEST_PG=1 python -m pytest -q tests/test_mart_correlations_pg.py

dbt kurulumu gerekmez: modeller Jinja ile derlenir, `ref()` geçici tablolara yönlendirilir
(fact_daily_prices → sentetik veri, mart_returns → derlenmiş modelin sonucu). Her şey tek transaction'da
yapılır ve geri alınır; veritabanında iz bırakmaz.
"""
import math
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bvt import returns as rt

pytestmark = pytest.mark.skipif(not os.getenv("BVT_TEST_PG"), reason="BVT_TEST_PG ayarlı değil (PostgreSQL gerekir)")
jinja2 = pytest.importorskip("jinja2")

MARTS = Path(__file__).resolve().parents[1] / "dbt" / "models" / "marts"
# Python'daki "bugün" SQL'deki now() ile aynı olmalı (is_complete); test verisi geçmişte olduğundan fark etmez
TODAY = date.today()


def render(model: str) -> str:
    template = jinja2.Environment().from_string((MARTS / f"{model}.sql").read_text(encoding="utf-8"))
    return template.render(ref=lambda name: f"pg_temp.{name}")


def synthetic_prices() -> pd.DataFrame:
    rows = []
    specs = [("USDTRY", 300, 34.0, 0.0, None), ("THYAO", 280, 250.0, 1.3, 1_000_000), ("XAUTRY_GRAM", 300, 2900.0, 2.1, None),
             ("XU100", 3, 9500.0, 0.0, None)]
    for code, n, base, phase, volume in specs:
        day = date(2024, 1, 1)
        for i in range(n):
            while day.weekday() >= 5 or (code == "THYAO" and day in (date(2024, 4, 10), date(2024, 4, 11))):
                day += timedelta(days=1)  # hafta sonu + yalnızca BIST'te bayram tatili
            price = base * (1 + 0.05 * math.sin(i / 4 + phase) + 0.001 * i + 0.01 * math.cos(i * phase))
            rows.append((code, day, round(price, 6), volume and volume + i))
            day += timedelta(days=1)
    return pd.DataFrame(rows, columns=["instrument_code", "price_date", "adj_close", "volume"])


@pytest.fixture
def conn():
    from bvt.db import connect

    with connect() as c:
        yield c
        c.rollback()


def _query(cur, sql: str) -> pd.DataFrame:
    cur.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def _floats(values) -> pd.Series:
    """numeric (Decimal), None, NaN, pd.NA karışık sütunu float'a çevirir."""
    return pd.Series([math.nan if v is None or pd.isna(v) else float(v) for v in values], dtype="float64")


def _assert_close(merged: pd.DataFrame, cols, rel=1e-9):
    for col in cols:
        got, want = _floats(merged[f"{col}_sql"]), _floats(merged[f"{col}_py"])
        assert (got.isna() == want.isna()).all(), col
        assert got.dropna().tolist() == pytest.approx(want.dropna().tolist(), rel=rel, abs=1e-9), col


def test_sql_matches_python_reference(conn):
    prices = synthetic_prices()
    conn.execute("""
        CREATE TEMP TABLE fact_daily_prices (
            instrument_key text, date_key integer, instrument_code text, price_date date,
            asset_class text, adj_close numeric(18, 6), volume bigint
        ) ON COMMIT DROP""")
    with conn.cursor() as cur:
        with cur.copy("COPY pg_temp.fact_daily_prices FROM STDIN") as copy:
            for r in prices.itertuples(index=False):
                copy.write_row((r.instrument_code.lower(), int(r.price_date.strftime("%Y%m%d")), r.instrument_code,
                                r.price_date, "stock", r.adj_close, None if pd.isna(r.volume) else int(r.volume)))
        cur.execute(f"CREATE TEMP TABLE mart_returns ON COMMIT DROP AS {render('mart_returns')}")
        sql_returns = _query(cur, "select * from pg_temp.mart_returns")
        sql_periods = _query(cur, render("mart_period_returns"))
        sql_corr = _query(cur, render("mart_correlations"))

    py_returns = rt.daily_returns(prices)

    # Günlük getiriler
    assert len(sql_returns) == len(prices)
    m = sql_returns.merge(py_returns, on=["instrument_code", "price_date"], suffixes=("_sql", "_py"))
    assert len(m) == len(prices)
    assert (m.obs_index_sql == m.obs_index_py).all()
    assert (m.days_since_prev_sql.fillna(-1).astype(int) == m.days_since_prev_py.fillna(-1).astype(int)).all()
    _assert_close(m, ["simple_return", "log_return", "cumulative_return"])

    # Haftalık / aylık özet
    py_periods = rt.period_summary(py_returns, today=TODAY)
    assert len(sql_periods) == len(py_periods)
    keys = ["instrument_code", "period_type", "period_start_date"]
    p = sql_periods.merge(py_periods, on=keys, suffixes=("_sql", "_py"))
    assert len(p) == len(py_periods)
    for col in ("period_end_date", "first_trading_date", "last_trading_date", "n_trading_days", "is_complete"):
        assert (p[f"{col}_sql"] == p[f"{col}_py"]).all(), col
    _assert_close(p, ["first_close", "last_close", "max_close", "min_close", "prev_period_close", "period_return",
                      "period_log_return", "volatility", "total_volume"])

    # Korelasyonlar (XU100'ün 2 getirisi var: pencere dolmaz ama satırları vardır)
    py_corr = rt.rolling_correlations(py_returns)
    assert len(sql_corr) == len(py_corr)
    assert (sql_corr.instrument_a < sql_corr.instrument_b).all()
    c = sql_corr.merge(py_corr, on=["instrument_a", "instrument_b", "price_date"], suffixes=("_sql", "_py"))
    assert len(c) == len(py_corr)
    assert (c.pair_obs_index_sql == c.pair_obs_index_py).all()
    _assert_close(c, [f"corr_{n}d" for n in rt.CORRELATION_WINDOWS], rel=1e-7)
    assert c.corr_252d_sql.notna().any()  # en az bir çiftte yıllık pencere doldu
