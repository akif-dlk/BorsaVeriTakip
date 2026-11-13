from datetime import date

import numpy as np
import pandas as pd
import pytest

from bvt import fetch_stocks


def _yf_frame(tickers, days=3, start="2024-01-02"):
    idx = pd.date_range(start, periods=days, freq="B", name="Date")
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    data = np.tile([100.0, 105.0, 98.0, 102.0, 101.5, 1_500_000], (days, len(tickers)))
    return pd.DataFrame(data, index=idx, columns=cols)


def test_default_tickers_include_index_and_strip_suffix():
    t = fetch_stocks.default_tickers()
    assert t["XU100"] == "XU100.IS"
    assert t["THYAO"] == "THYAO.IS"
    assert len(t) == len(fetch_stocks.config.BIST_TICKERS) + 1
    assert all(not s.endswith(".IS") for s in t)


def test_select_tickers_subset_and_unknown():
    assert fetch_stocks.select_tickers(["thyao", "XU100.IS"]) == {"THYAO": "THYAO.IS", "XU100": "XU100.IS"}
    with pytest.raises(ValueError, match="Bilinmeyen"):
        fetch_stocks.select_tickers(["NOPE"])


def test_parse_yf_frame_long_format_with_adj_close_and_volume():
    raw = _yf_frame(["THYAO.IS", "XU100.IS"])
    df = fetch_stocks.parse_yf_frame(raw, {"THYAO": "THYAO.IS", "XU100": "XU100.IS"})
    assert list(df.columns) == fetch_stocks.COLUMNS
    assert len(df) == 6
    row = df.iloc[0]
    assert row["symbol"] == "THYAO" and row["price_date"] == date(2024, 1, 2)
    assert row["adj_close"] == pytest.approx(101.5) and row["volume"] == 1_500_000


def test_parse_yf_frame_single_ticker_flat_columns():
    raw = _yf_frame(["GARAN.IS"]).droplevel(0, axis=1)
    df = fetch_stocks.parse_yf_frame(raw, {"GARAN": "GARAN.IS"})
    assert len(df) == 3 and set(df["symbol"]) == {"GARAN"}


def test_parse_yf_frame_missing_ticker_is_skipped():
    raw = _yf_frame(["THYAO.IS"])
    df = fetch_stocks.parse_yf_frame(raw, {"THYAO": "THYAO.IS", "GARAN": "GARAN.IS"})
    assert set(df["symbol"]) == {"THYAO"}


def _row(symbol, d, close, high=105.0, low=98.0, volume=1000.0):
    return [symbol, d, "yfinance", 100.0, high, low, close, close, volume]


def test_clean_rules():
    df = pd.DataFrame(
        [
            _row("THYAO", date(2024, 1, 2), 102.0),
            _row("THYAO", date(2024, 1, 2), 103.0),  # tekrar -> son kalır
            _row("THYAO", date(2024, 1, 3), np.nan),  # işlem yok
            _row("THYAO", date(2024, 1, 4), 0.0),  # geçersiz
            _row("GARAN", date(2024, 1, 2), 50.0, high=40.0, low=60.0),  # high < low
            _row("XU100", date(2024, 1, 2), 8000.0, volume=-5),  # negatif hacim
            _row("XU100", date(2024, 1, 3), 8100.0, volume=np.nan),
        ],
        columns=fetch_stocks.COLUMNS,
    )
    out = fetch_stocks.clean(df).set_index(["symbol", "price_date"])
    assert len(out) == 4
    assert out.loc[("THYAO", date(2024, 1, 2)), "close"] == pytest.approx(103.0)
    garan = out.loc[("GARAN", date(2024, 1, 2))]
    assert garan["high"] is None and garan["open"] is None and garan["close"] == pytest.approx(50.0)
    assert out.loc[("XU100", date(2024, 1, 2)), "volume"] is None
    assert out.loc[("XU100", date(2024, 1, 3)), "volume"] is None
    vol = out.loc[("THYAO", date(2024, 1, 2)), "volume"]
    assert vol == 1000 and isinstance(vol, int)  # BIGINT'e uygun Python int


def test_chunk_ranges_cover_range_without_overlap():
    chunks = list(fetch_stocks.chunk_ranges(date(2024, 1, 1), date(2024, 1, 10), chunk_days=4))
    assert chunks == [
        (date(2024, 1, 1), date(2024, 1, 4)),
        (date(2024, 1, 5), date(2024, 1, 8)),
        (date(2024, 1, 9), date(2024, 1, 10)),
    ]
    assert list(fetch_stocks.chunk_ranges(date(2024, 1, 1), date(2024, 1, 1))) == [(date(2024, 1, 1),) * 2]
    with pytest.raises(ValueError):
        list(fetch_stocks.chunk_ranges(date(2024, 2, 1), date(2024, 1, 1)))


def test_fetch_range_single_bulk_request(monkeypatch):
    calls = []

    def fake_download(tickers, **kwargs):
        calls.append((tickers, kwargs))
        return _yf_frame(tickers)

    monkeypatch.setattr(fetch_stocks.yf, "download", fake_download)
    df = fetch_stocks.fetch_range(date(2024, 1, 2), date(2024, 1, 4))
    assert len(calls) == 1
    assert "XU100.IS" in calls[0][0] and len(calls[0][0]) == len(fetch_stocks.default_tickers())
    assert calls[0][1]["end"] == "2024-01-05" and calls[0][1]["auto_adjust"] is False
    assert len(df) == 3 * len(fetch_stocks.default_tickers())


def test_iter_chunks_one_request_per_chunk_with_pause(monkeypatch):
    starts, sleeps = [], []

    def fake_download(tickers, start, **kwargs):
        starts.append(start)
        return _yf_frame(tickers, start=start)

    monkeypatch.setattr(fetch_stocks.yf, "download", fake_download)
    tickers = {"THYAO": "THYAO.IS"}
    frames = list(fetch_stocks.iter_chunks(date(2024, 1, 1), date(2024, 1, 31), tickers, chunk_days=10,
                                           pause=1.5, sleep=sleeps.append))
    assert starts == ["2024-01-01", "2024-01-11", "2024-01-21", "2024-01-31"]
    assert sleeps == [1.5, 1.5, 1.5]  # ilk istekten önce bekleme yok
    assert len(frames) == 4


def test_iter_chunks_skips_empty_short_chunk_but_raises_on_long(monkeypatch):
    monkeypatch.setattr(fetch_stocks.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr("bvt.retry.time.sleep", lambda s: None)
    tickers = {"THYAO": "THYAO.IS"}
    # Cumartesi-pazar: veri yok, hata değil
    assert list(fetch_stocks.iter_chunks(date(2024, 1, 6), date(2024, 1, 7), tickers, pause=0)) == []
    with pytest.raises(fetch_stocks.FetchError):
        list(fetch_stocks.iter_chunks(date(2024, 1, 1), date(2024, 1, 31), tickers, pause=0))


class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, rows):
        self.sink.append((sql, list(rows)))


class _FakeConn:
    def __init__(self):
        self.executed, self.commits = [], 0

    def cursor(self):
        return _FakeCursor(self.executed)

    def commit(self):
        self.commits += 1


def test_load_upserts_rows():
    tickers = {"THYAO": "THYAO.IS", "XU100": "XU100.IS"}
    df = fetch_stocks.clean(fetch_stocks.parse_yf_frame(_yf_frame(list(tickers.values())), tickers))
    conn = _FakeConn()
    assert fetch_stocks.load(conn, df) == 6
    sql, rows = conn.executed[0]
    assert "raw.stock_prices" in sql and "ON CONFLICT (symbol, price_date, source) DO UPDATE" in sql
    assert len(rows[0]) == 9
    assert conn.commits == 1


def test_load_empty_is_noop():
    conn = _FakeConn()
    assert fetch_stocks.load(conn, pd.DataFrame(columns=fetch_stocks.COLUMNS)) == 0
    assert conn.executed == []


def test_parse_args():
    args = fetch_stocks.parse_args(["--end", "2024-03-31", "--days", "10", "--symbols", "THYAO", "XU100"])
    assert args.start == date(2024, 3, 21) and args.symbols == ["THYAO", "XU100"]
    with pytest.raises(SystemExit):
        fetch_stocks.parse_args(["--start", "2024-04-01", "--end", "2024-03-01"])


def test_dry_run_prints_without_db(monkeypatch, capsys):
    monkeypatch.setattr(fetch_stocks.yf, "download", lambda tickers, **k: _yf_frame(tickers))
    n = fetch_stocks.run(date(2024, 1, 2), date(2024, 1, 4), symbols=["THYAO"], dry_run=True)
    assert n == 3
    assert "THYAO" in capsys.readouterr().out
