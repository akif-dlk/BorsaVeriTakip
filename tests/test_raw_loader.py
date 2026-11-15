from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import FakeConn

from bvt import raw_loader
from bvt.raw_loader import FX_RATES, GOLD_PRICES, RAW_TABLES, STOCK_PRICES, LoadResult


def _fx_row(d, close, pair="USDTRY", source="yfinance"):
    return (pair, d, source, None, None, None, close)


def test_table_contracts_match_sql_primary_keys():
    assert FX_RATES.key == ("pair", "rate_date", "source")
    assert STOCK_PRICES.key == ("symbol", "price_date", "source")
    assert GOLD_PRICES.key == ("instrument", "price_date", "source")
    for t in RAW_TABLES.values():
        assert not set(t.key) & set(t.values)
        assert "fetched_at" not in t.columns and "inserted_at" not in t.columns


def test_upsert_sql_only_rewrites_changed_rows():
    sql = raw_loader.upsert_sql(STOCK_PRICES)
    assert sql.startswith("INSERT INTO raw.stock_prices AS t (symbol, price_date, source, open")
    assert ("SELECT symbol, price_date, source, open, high, low, close, adj_close, volume "
            "FROM _stage_raw_stock_prices") in sql
    assert "ON CONFLICT (symbol, price_date, source) DO UPDATE SET" in sql
    assert "adj_close = EXCLUDED.adj_close" in sql and "fetched_at = now()" in sql
    assert ("WHERE (t.open, t.high, t.low, t.close, t.adj_close, t.volume) IS DISTINCT FROM "
            "(EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.adj_close, EXCLUDED.volume)") in sql
    assert sql.rstrip().endswith("RETURNING (t.xmax = 0) AS inserted")
    # Anahtar sütunları asla güncellenmez, inserted_at ilk değerinde kalır
    assert "symbol = EXCLUDED" not in sql and "inserted_at" not in sql


def test_stage_keeps_defaults_and_is_dropped():
    create = raw_loader.create_stage_sql(FX_RATES)
    assert "LIKE raw.fx_rates INCLUDING DEFAULTS" in create and "ON COMMIT DROP" in create
    assert raw_loader.copy_sql(FX_RATES) == \
        "COPY _stage_raw_fx_rates (pair, rate_date, source, open, high, low, close) FROM STDIN"


def test_dedupe_keeps_last_and_first_seen_order():
    rows = [_fx_row(date(2024, 1, 2), 30.0), _fx_row(date(2024, 1, 3), 30.1), _fx_row(date(2024, 1, 2), 30.5)]
    out = raw_loader.dedupe(rows, key_len=3)
    assert [r[1] for r in out] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert out[0][-1] == 30.5


def test_dedupe_rejects_null_key():
    with pytest.raises(ValueError):
        raw_loader.dedupe([(None, date(2024, 1, 2), "yfinance", None, None, None, 30.0)], key_len=3)


def test_to_python_converts_numpy_and_nan():
    assert type(raw_loader.to_python(np.float64(1.5))) is float
    assert type(raw_loader.to_python(np.int64(7))) is int
    assert raw_loader.to_python(float("nan")) is None
    assert raw_loader.to_python(np.float64("nan")) is None
    assert raw_loader.to_python(pd.NaT) is None
    assert raw_loader.to_python(date(2024, 1, 2)) == date(2024, 1, 2)


def test_load_rows_counts_inserted_updated_unchanged():
    rows = [_fx_row(date(2024, 1, d), 30.0 + d) for d in range(2, 6)]  # 4 satır
    # Sunucu 2 satırı yeni, 1'ini güncellenmiş döndürsün; 4. satır değişmemiş (RETURNING'e girmez)
    conn = FakeConn(returning=[True, False, True])
    result = raw_loader.load_rows(conn, FX_RATES, rows)
    assert (result.inserted, result.updated, result.unchanged) == (2, 1, 1)
    assert result.total == 4 and result.written == 3
    assert conn.executed[0].startswith("CREATE TEMP TABLE _stage_raw_fx_rates")
    assert conn.executed[-1] == "DROP TABLE IF EXISTS _stage_raw_fx_rates"
    assert len(conn.copied) == 4 and conn.commits == 1


def test_duplicate_rows_in_batch_are_sent_once():
    rows = [_fx_row(date(2024, 1, 2), 30.0), _fx_row(date(2024, 1, 2), 30.0)]
    conn = FakeConn()
    assert raw_loader.load_rows(conn, FX_RATES, rows).total == 1
    assert len(conn.copied) == 1


def test_second_identical_load_reports_unchanged():
    rows = [_fx_row(date(2024, 1, 2), 30.0)]
    first = raw_loader.load_rows(FakeConn(returning=[True]), FX_RATES, rows)
    second = raw_loader.load_rows(FakeConn(returning=[]), FX_RATES, rows)  # hiçbir satır yeniden yazılmadı
    assert (first.inserted, second.inserted, second.updated, second.unchanged) == (1, 0, 0, 1)


def test_load_rows_without_commit_leaves_transaction_open():
    conn = FakeConn()
    raw_loader.load_rows(conn, FX_RATES, [_fx_row(date(2024, 1, 2), 30.0)], commit=False)
    assert conn.commits == 0


def test_load_frame_empty_and_missing_columns():
    conn = FakeConn()
    assert raw_loader.load_frame(conn, FX_RATES, pd.DataFrame()).total == 0
    assert conn.executed == []
    with pytest.raises(ValueError, match="eksik"):
        raw_loader.load_frame(conn, FX_RATES, pd.DataFrame({"pair": ["USDTRY"], "close": [30.0]}))


def test_load_result_addition_and_str():
    total = LoadResult(1, 2, 3) + LoadResult(4, 0, 1)
    assert (total.inserted, total.updated, total.unchanged, total.total) == (5, 2, 4, 11)
    assert str(total) == "5 yeni, 2 güncellendi, 4 değişmedi"
