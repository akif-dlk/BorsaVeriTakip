from datetime import date

import numpy as np
import pandas as pd
import pytest

from bvt import fetch_fx
from bvt.retry import retry


def _yf_frame(tickers, days=3, close=None):
    idx = pd.date_range("2024-01-02", periods=days, freq="B", name="Date")
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    data = np.tile([30.0, 31.0, 29.0, 30.5, 30.5, 0], (days, len(tickers)))
    df = pd.DataFrame(data, index=idx, columns=cols)
    if close is not None:
        df[(tickers[0], "Close")] = close
    return df


def test_parse_yf_frame_long_format():
    raw = _yf_frame(["USDTRY=X", "EURTRY=X"])
    df = fetch_fx.parse_yf_frame(raw, {"USDTRY": "USDTRY=X", "EURTRY": "EURTRY=X"})
    assert list(df.columns) == fetch_fx.COLUMNS
    assert len(df) == 6
    assert set(df["pair"]) == {"USDTRY", "EURTRY"}
    assert (df["source"] == "yfinance").all()
    assert df["rate_date"].iloc[0] == date(2024, 1, 2)


def test_parse_yf_frame_missing_ticker_is_skipped():
    raw = _yf_frame(["USDTRY=X"])
    df = fetch_fx.parse_yf_frame(raw, {"USDTRY": "USDTRY=X", "EURTRY": "EURTRY=X"})
    assert set(df["pair"]) == {"USDTRY"}


def test_clean_drops_invalid_and_duplicates():
    df = pd.DataFrame(
        [
            ["USDTRY", date(2024, 1, 2), "yfinance", 30, 31, 29, 30.5],
            ["USDTRY", date(2024, 1, 2), "yfinance", 30, 31, 29, 30.6],  # tekrar -> son kalır
            ["USDTRY", date(2024, 1, 3), "yfinance", 30, 31, 29, np.nan],  # boş kapanış
            ["USDTRY", date(2024, 1, 4), "yfinance", 30, 31, 29, -1],  # negatif
            ["EURTRY", date(2024, 1, 2), "yfinance", 33, 32, 34, 33.1],  # high < low
        ],
        columns=fetch_fx.COLUMNS,
    )
    out = fetch_fx.clean(df)
    assert len(out) == 2
    usd = out[out["pair"] == "USDTRY"].iloc[0]
    assert usd["close"] == pytest.approx(30.6)
    eur = out[out["pair"] == "EURTRY"].iloc[0]
    assert eur["high"] is None and eur["low"] is None and eur["close"] == pytest.approx(33.1)


def test_clean_converts_nan_to_none():
    df = pd.DataFrame([["USDTRY", date(2024, 1, 2), "evds", None, None, None, 30.1]], columns=fetch_fx.COLUMNS)
    out = fetch_fx.clean(df)
    assert out.iloc[0]["open"] is None


def test_fetch_yfinance_uses_single_bulk_request(monkeypatch):
    calls = []

    def fake_download(tickers, **kwargs):
        calls.append((tickers, kwargs))
        return _yf_frame(["USDTRY=X", "EURTRY=X"])

    monkeypatch.setattr(fetch_fx.yf, "download", fake_download)
    df = fetch_fx.fetch_yfinance(date(2024, 1, 2), date(2024, 1, 4))
    assert len(calls) == 1
    assert calls[0][0] == ["USDTRY=X", "EURTRY=X"]
    assert calls[0][1]["end"] == "2024-01-05"  # end dahil olsun diye +1 gün
    assert len(df) == 6


def test_fetch_yfinance_retries_on_empty(monkeypatch):
    responses = [pd.DataFrame(), _yf_frame(["USDTRY=X", "EURTRY=X"])]
    monkeypatch.setattr(fetch_fx.yf, "download", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr("bvt.retry.time.sleep", lambda s: None)
    df = fetch_fx.fetch_yfinance(date(2024, 1, 2), date(2024, 1, 4))
    assert len(df) == 6 and not responses


def test_parse_evds_skips_holidays():
    payload = {
        "items": [
            {"Tarih": "02-01-2024", "TP_DK_USD_A": "29.7", "TP_DK_EUR_A": "32.6"},
            {"Tarih": "06-01-2024", "TP_DK_USD_A": None, "TP_DK_EUR_A": None},  # cumartesi
        ]
    }
    df = fetch_fx.parse_evds(payload)
    assert len(df) == 2
    assert set(df["source"]) == {"evds"}
    assert df.set_index("pair").loc["USDTRY", "close"] == pytest.approx(29.7)


def test_fetch_all_survives_evds_failure(monkeypatch):
    good = fetch_fx.clean(fetch_fx.parse_yf_frame(_yf_frame(["USDTRY=X", "EURTRY=X"]), fetch_fx.config.FX_PAIRS))
    monkeypatch.setattr(fetch_fx, "fetch_yfinance", lambda s, e: good)

    def boom(*a):
        raise fetch_fx.FetchError("EVDS kapalı")

    monkeypatch.setattr(fetch_fx, "fetch_evds", boom)
    df = fetch_fx.fetch_all(date(2024, 1, 2), date(2024, 1, 4), evds_key="dummy")
    assert len(df) == 6


def test_fetch_all_rejects_reversed_range():
    with pytest.raises(ValueError):
        fetch_fx.fetch_all(date(2024, 2, 1), date(2024, 1, 1))


def test_load_upserts_rows(fake_conn):
    df = fetch_fx.clean(fetch_fx.parse_yf_frame(_yf_frame(["USDTRY=X", "EURTRY=X"]), fetch_fx.config.FX_PAIRS))
    result = fetch_fx.load(fake_conn, df)
    assert result.inserted == 6 and result.total == 6
    assert any("INSERT INTO raw.fx_rates" in sql and "ON CONFLICT (pair, rate_date, source) DO UPDATE" in sql
               for sql in fake_conn.executed)
    assert len(fake_conn.copied) == 6 and len(fake_conn.copied[0]) == 7
    assert fake_conn.commits == 1


def test_load_empty_is_noop(fake_conn):
    assert fetch_fx.load(fake_conn, pd.DataFrame(columns=fetch_fx.COLUMNS)).total == 0
    assert fake_conn.executed == []


def test_parse_args_default_window():
    args = fetch_fx.parse_args(["--end", "2024-03-31", "--days", "10"])
    assert args.start == date(2024, 3, 21) and args.end == date(2024, 3, 31)


def test_retry_gives_up_after_attempts():
    sleeps, calls = [], []

    @retry(attempts=3, base_delay=1, sleep=sleeps.append)
    def flaky():
        calls.append(1)
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        flaky()
    assert len(calls) == 3 and len(sleeps) == 2
    assert 1 <= sleeps[0] < 1.6 and 2 <= sleeps[1] < 2.6  # üstel bekleme


def test_fetch_evds_empty_base_url_falls_back_to_default(monkeypatch):
    # .env.example / docker-compose boş `EVDS_BASE_URL=` geçirir; adres "/" olmamalı.
    seen = {}
    monkeypatch.setenv("EVDS_BASE_URL", "")
    monkeypatch.setattr(fetch_fx, "_get_evds", lambda url, key: seen.setdefault("url", url) and {"items": []})
    fetch_fx.fetch_evds(date(2024, 1, 2), date(2024, 1, 3), "dummy")
    assert seen["url"].startswith(fetch_fx.EVDS_DEFAULT_URL + "series=")
