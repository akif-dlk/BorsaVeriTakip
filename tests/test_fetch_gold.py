from datetime import date

import numpy as np
import pandas as pd
import pytest

from bvt import config, fetch_gold

GRAMS = config.TROY_OUNCE_GRAMS


def _frame(ticker_values: dict[str, list[float]], start="2024-01-02", freq="D"):
    """{ticker: [kapanışlar]} -> yf.download benzeri (ticker, alan) MultiIndex'li çerçeve. NaN = işlem yok."""
    n = max(len(v) for v in ticker_values.values())
    idx = pd.date_range(start, periods=n, freq=freq, name="Date")
    parts = {}
    for ticker, closes in ticker_values.items():
        c = np.array(closes + [np.nan] * (n - len(closes)), dtype=float)
        for field, val in (("Open", c * 0.99), ("High", c * 1.01), ("Low", c * 0.98), ("Close", c),
                           ("Adj Close", c), ("Volume", np.zeros(n))):
            parts[(ticker, field)] = val
    return pd.DataFrame(parts, index=idx)


def test_gram_formula():
    ounce = pd.DataFrame({"open": [2000.0], "high": [2010.0], "low": [1990.0], "close": [2005.0]},
                         index=pd.DatetimeIndex(["2024-01-02"]))
    usd = pd.Series([30.0], index=pd.DatetimeIndex(["2024-01-02"]))
    gram = fetch_gold.gram_from_ounce(ounce, usd)
    assert gram.loc["2024-01-02", "close"] == pytest.approx(2005.0 * 30.0 / GRAMS)
    assert gram.loc["2024-01-02", "high"] == pytest.approx(2010.0 * 30.0 / GRAMS)
    assert gram.loc["2024-01-02", "usdtry"] == 30.0


def test_gram_uses_last_known_rate_within_tolerance():
    ounce = pd.DataFrame({c: [2000.0, 2000.0, 2000.0] for c in fetch_gold.OHLC},
                         index=pd.DatetimeIndex(["2024-01-02", "2024-01-04", "2024-01-20"]))
    # Kur yalnızca 1 Ocak'ta var: 2 ve 4 Ocak tolerans içinde (<=4 gün), 20 Ocak dışarıda kalır
    usd = pd.Series([30.0], index=pd.DatetimeIndex(["2024-01-01"]))
    gram = fetch_gold.gram_from_ounce(ounce, usd)
    assert list(gram.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-04"]
    assert (gram["usdtry"] == 30.0).all()


def test_gram_never_uses_future_rate():
    ounce = pd.DataFrame({c: [2000.0] for c in fetch_gold.OHLC}, index=pd.DatetimeIndex(["2024-01-02"]))
    usd = pd.Series([31.0], index=pd.DatetimeIndex(["2024-01-03"]))
    assert fetch_gold.gram_from_ounce(ounce, usd).empty


def test_extract_ticker_strips_tz_and_drops_empty_days():
    raw = _frame({"GC=F": [2000.0, np.nan, 2010.0], "USDTRY=X": [30.0, 30.1, 30.2]})
    raw.index = raw.index.tz_localize("America/New_York")
    out = fetch_gold.extract_ticker(raw, "GC=F")
    assert out.index.tz is None
    assert list(out.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-04"]
    assert fetch_gold.extract_ticker(raw, "NOPE").empty


def test_fetch_range_single_bulk_request_with_lookback(monkeypatch):
    calls = []

    def fake_download(tickers, start, end, **kwargs):
        calls.append((tickers, start, end))
        # 7 gün geriye + istenen 3 gün; GC=F hafta sonu (6-7 Ocak) yok, kur her gün var
        gc = [2000.0] * 7 + [np.nan, np.nan] + [2040.0]  # 30 Ara..8 Oca
        return _frame({"GC=F": gc, "USDTRY=X": [30.0] * 10}, start="2023-12-30")

    monkeypatch.setattr(fetch_gold.yf, "download", fake_download)
    df = fetch_gold.fetch_range(date(2024, 1, 6), date(2024, 1, 8))
    assert len(calls) == 1
    assert calls[0][0] == ["GC=F", "USDTRY=X"]
    assert calls[0][1] == "2023-12-30" and calls[0][2] == "2024-01-09"
    assert list(df.columns) == fetch_gold.COLUMNS
    # Yalnızca istenen aralık; 6-7 Ocak'ta ons yok -> sadece 8 Ocak (ons + gram)
    assert set(df["price_date"]) == {date(2024, 1, 8)}
    ons = df[df["instrument"] == fetch_gold.OUNCE].iloc[0]
    gram = df[df["instrument"] == fetch_gold.GRAM].iloc[0]
    assert ons["currency"] == "USD" and ons["source"] == "yfinance" and ons["usdtry"] is None
    assert gram["currency"] == "TRY" and gram["source"] == "derived"
    assert gram["close"] == pytest.approx(round(ons["close"] * 30.0 / GRAMS, 6))


def test_fetch_range_without_usdtry_returns_ounce_only(monkeypatch):
    monkeypatch.setattr(fetch_gold.yf, "download", lambda *a, **k: _frame({"GC=F": [2000.0] * 10},
                                                                           start="2023-12-26"))
    df = fetch_gold.fetch_range(date(2024, 1, 2), date(2024, 1, 4))
    assert set(df["instrument"]) == {fetch_gold.OUNCE} and len(df) == 3


def test_fetch_range_raises_when_no_gold(monkeypatch):
    monkeypatch.setattr(fetch_gold.yf, "download", lambda *a, **k: _frame({"USDTRY=X": [30.0] * 5}))
    with pytest.raises(fetch_gold.FetchError):
        fetch_gold.fetch_range(date(2024, 1, 2), date(2024, 1, 4))


def test_fetch_range_retries_on_empty(monkeypatch):
    responses = [pd.DataFrame(), _frame({"GC=F": [2000.0] * 10, "USDTRY=X": [30.0] * 10}, start="2023-12-26")]
    monkeypatch.setattr(fetch_gold.yf, "download", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr("bvt.retry.time.sleep", lambda s: None)
    df = fetch_gold.fetch_range(date(2024, 1, 2), date(2024, 1, 4))
    assert len(df) == 6 and not responses


def _row(instrument, d, close, high=None, low=None, usdtry=None):
    source, cur = ("derived", "TRY") if instrument == fetch_gold.GRAM else ("yfinance", "USD")
    return [instrument, d, source, cur, close, high if high is not None else close,
            low if low is not None else close, close, usdtry]


def test_clean_rules():
    df = pd.DataFrame(
        [
            _row(fetch_gold.OUNCE, date(2024, 1, 2), 2000.0),
            _row(fetch_gold.OUNCE, date(2024, 1, 2), 2001.0),  # tekrar -> son kalır
            _row(fetch_gold.OUNCE, date(2024, 1, 3), np.nan),  # boş kapanış
            _row(fetch_gold.OUNCE, date(2024, 1, 4), -5.0),  # geçersiz
            _row(fetch_gold.GRAM, date(2024, 1, 2), 1900.0, high=1800.0, low=2000.0, usdtry=30.0),  # high < low
        ],
        columns=fetch_gold.COLUMNS,
    )
    out = fetch_gold.clean(df).set_index(["instrument", "price_date"])
    assert len(out) == 2
    assert out.loc[(fetch_gold.OUNCE, date(2024, 1, 2)), "close"] == pytest.approx(2001.0)
    assert out.loc[(fetch_gold.OUNCE, date(2024, 1, 2)), "usdtry"] is None
    g = out.loc[(fetch_gold.GRAM, date(2024, 1, 2))]
    assert g["high"] is None and g["open"] is None and g["close"] == pytest.approx(1900.0)


def test_iter_chunks_skips_short_empty_chunk(monkeypatch):
    monkeypatch.setattr(fetch_gold.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr("bvt.retry.time.sleep", lambda s: None)
    assert list(fetch_gold.iter_chunks(date(2024, 1, 6), date(2024, 1, 7), pause=0)) == []
    with pytest.raises(fetch_gold.FetchError):
        list(fetch_gold.iter_chunks(date(2024, 1, 1), date(2024, 1, 31), pause=0))


def test_load_upserts_rows(fake_conn):
    df = fetch_gold.clean(pd.DataFrame([_row(fetch_gold.OUNCE, date(2024, 1, 2), 2000.0),
                                        _row(fetch_gold.GRAM, date(2024, 1, 2), 1929.0, usdtry=30.0)],
                                       columns=fetch_gold.COLUMNS))
    assert fetch_gold.load(fake_conn, df).inserted == 2
    assert any("INSERT INTO raw.gold_prices" in sql and "ON CONFLICT (instrument, price_date, source) DO UPDATE"
               in sql for sql in fake_conn.executed)
    assert len(fake_conn.copied[0]) == 9 and fake_conn.commits == 1
    ounce_row = next(r for r in fake_conn.copied if r[0] == fetch_gold.OUNCE)
    assert ounce_row[-1] is None  # ons satirinda usdtry NULL


def test_load_empty_is_noop(fake_conn):
    assert fetch_gold.load(fake_conn, pd.DataFrame(columns=fetch_gold.COLUMNS)).total == 0
    assert fake_conn.executed == []


def test_parse_args():
    args = fetch_gold.parse_args(["--end", "2024-03-31", "--days", "10"])
    assert args.start == date(2024, 3, 21)
    with pytest.raises(SystemExit):
        fetch_gold.parse_args(["--start", "2024-04-01", "--end", "2024-03-01"])


def test_dry_run_prints_without_db(monkeypatch, capsys):
    monkeypatch.setattr(fetch_gold.yf, "download",
                        lambda *a, **k: _frame({"GC=F": [2000.0] * 10, "USDTRY=X": [30.0] * 10}, start="2023-12-26"))
    n = fetch_gold.run(date(2024, 1, 2), date(2024, 1, 4), dry_run=True)
    assert n == 6
    out = capsys.readouterr().out
    assert fetch_gold.GRAM in out and fetch_gold.OUNCE in out
