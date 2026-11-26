import pandas as pd
import pytest
from ta.momentum import RSIIndicator

from config.settings import VERI_YOK
from tools import fundamental_data, market_data, news_sentiment


# ---------- market_data ----------
def test_normalize_ticker():
    assert market_data.normalize_ticker("thyao") == "THYAO.IS"
    assert market_data.normalize_ticker("THYAO.IS") == "THYAO.IS"
    assert market_data.normalize_ticker("^XU100") == "^XU100"


def test_indicators_full(ohlcv):
    ind = market_data.compute_indicators(ohlcv)
    assert ind["status"] == "ok"
    assert ind["close"] == round(ohlcv["close"].iloc[-1], 4)
    expected_rsi = RSIIndicator(ohlcv["close"], 14).rsi().iloc[-1]
    assert ind["rsi14"] == pytest.approx(expected_rsi, abs=0.01)
    assert ind["ma20"] == pytest.approx(ohlcv["close"].tail(20).mean(), rel=1e-6)
    assert ind["ma200"] == pytest.approx(ohlcv["close"].tail(200).mean(), rel=1e-6)
    assert ind["volume_anomaly"] is True
    assert ind["volume_ratio"] == pytest.approx(3.0, abs=0.01)
    assert ind["levels"]["support_60d"] <= ind["close"] <= ind["levels"]["resistance_60d"]
    import json; json.dumps(ind)  # tamamen JSON-serileştirilebilir olmalı
    expected_1d = (ohlcv["close"].iloc[-1] / ohlcv["close"].iloc[-2] - 1) * 100
    assert ind["change_1d_pct"] == pytest.approx(expected_1d, abs=0.01)


def test_indicators_short_series_marks_veri_yok(ohlcv):
    ind = market_data.compute_indicators(ohlcv.tail(30))
    assert ind["status"] == "ok"
    assert ind["ma200"] == VERI_YOK
    assert ind["ma50"] == VERI_YOK
    assert ind["above_ma200"] == VERI_YOK
    assert isinstance(ind["rsi14"], float)


def test_indicators_empty():
    assert market_data.compute_indicators(pd.DataFrame()) == {"status": VERI_YOK}


def test_sentiment_metrics(ohlcv):
    s = market_data.compute_sentiment_metrics(market_data.compute_indicators(ohlcv))
    assert s["rsi_zone"] in {"asiri_alim", "asiri_satim", "notr"}
    assert isinstance(s["bollinger_position"], float)
    assert market_data.compute_sentiment_metrics({"status": VERI_YOK}) == {"status": VERI_YOK}


def test_snapshot_handles_fetch_failure(monkeypatch):
    monkeypatch.setattr(market_data, "fetch_price_history", lambda t, p="1y": pd.DataFrame())
    snap = market_data.get_market_snapshot("XXXX")
    assert snap["indicators"]["status"] == VERI_YOK
    assert snap["sentiment_metrics"]["status"] == VERI_YOK


# ---------- fundamental_data ----------
def test_fundamentals_derived_values():
    info = {"trailingPE": 5.5, "totalDebt": 1000.0, "totalCash": 400.0, "ebitda": 300.0, "sector": "Industrials"}
    f = fundamental_data.get_fundamentals("THYAO", info_fetcher=lambda t: info, mcp_fetcher=lambda t: {})
    assert f["status"] == "ok"
    assert f["ticker"] == "THYAO.IS"
    assert f["pe_ratio"] == 5.5
    assert f["net_debt"] == 600
    assert f["net_debt_to_ebitda"] == 2.0
    assert f["pb_ratio"] == VERI_YOK
    assert "pb_ratio" in f["missing_fields"]


def test_fundamentals_source_down():
    f = fundamental_data.get_fundamentals("THYAO", info_fetcher=lambda t: {}, mcp_fetcher=lambda t: {})
    assert f["status"] == VERI_YOK


# ---------- news_sentiment ----------
def test_news_ok():
    fake = lambda url: [{"title": f"Haber {i}", "link": "x", "published": "2026-01-01", "source": "s"} for i in range(20)]
    n = news_sentiment.get_company_news("THYAO.IS", limit=5, parser=fake)
    assert n["status"] == "ok"
    assert len(n["company_news"]) == 5
    assert n["news_count"] == 10


def test_news_source_down_no_fabrication():
    n = news_sentiment.get_company_news("THYAO.IS", parser=lambda url: None)
    assert n["status"] == VERI_YOK
    assert n["company_news"] == VERI_YOK
    assert n["kap_news"] == VERI_YOK
    m = news_sentiment.get_macro_news(parser=lambda url: None)
    assert m["status"] == VERI_YOK


# ---------- gerçek ağ (opsiyonel) ----------
@pytest.mark.network
def test_live_snapshot():
    snap = market_data.get_market_snapshot("THYAO.IS")
    assert snap["indicators"]["status"] == "ok"
    assert isinstance(snap["indicators"]["rsi14"], float)


def test_fundamentals_sanity_filter():
    info = {"trailingPE": 5.0, "priceToBook": 19.05, "enterpriseToEbitda": 222.5}
    f = fundamental_data.get_fundamentals("THYAO", info_fetcher=lambda t: info, mcp_fetcher=lambda t: {})
    assert f["pb_ratio"] == VERI_YOK and f["ev_ebitda"] == VERI_YOK and f["pe_ratio"] == 5.0
    assert len(f["suspicious_values_removed"]) == 2


# ---------- seans içi (yarım) mum ----------
def _intraday_df(last_vol: float, prev_vol: float = 1000.0, n: int = 40):
    from datetime import date
    idx = pd.bdate_range(end=date(2026, 11, 24), periods=n)  # son mum: Salı 24.11.2026
    vol = [1000.0] * (n - 2) + [prev_vol, last_vol]
    close = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame({"open": close, "high": [c + 1 for c in close], "low": [c - 1 for c in close],
                         "close": close, "volume": vol}, index=idx)


def _tr(h, m):
    from datetime import datetime
    return datetime(2026, 11, 24, h, m, tzinfo=market_data._TR)


def test_session_progress():
    from datetime import date
    d = date(2026, 11, 24)
    assert market_data.session_progress(d, _tr(9, 30)) == 0.0
    assert market_data.session_progress(d, _tr(14, 0)) == pytest.approx(0.5)
    assert market_data.session_progress(d, _tr(18, 15)) is None  # seans kapandı
    assert market_data.session_progress(date(2026, 11, 23), _tr(14, 0)) is None  # dünkü mum tamam


def test_early_session_uses_previous_full_day_volume():
    # 10:30'da bugün 80 lot (ortalamanın %8'i) -> "katılım yok" demek yanlış; dünün hacmi (1500) kullanılır
    ind = market_data.compute_indicators(_intraday_df(80.0, prev_vol=1500.0), now=_tr(10, 30))
    assert ind["intraday_bar"] is True
    assert ind["volume_basis"] == "onceki_tam_gun"
    assert ind["volume_ratio"] == pytest.approx(1.5, abs=0.01)
    assert ind["session_elapsed_pct"] == 6


def test_midsession_volume_is_prorated():
    # 14:00 = seansın yarısı; 600 lot / (1000 * 0.5) = 1.2
    ind = market_data.compute_indicators(_intraday_df(600.0), now=_tr(14, 0))
    assert ind["volume_basis"] == "seans_ici_orantili_tahmin"
    assert ind["volume_ratio"] == pytest.approx(1.2, abs=0.01)


def test_after_close_is_full_day():
    ind = market_data.compute_indicators(_intraday_df(600.0), now=_tr(18, 30))
    assert ind["intraday_bar"] is False
    assert ind["volume_basis"] == "tam_gun"
    assert ind["volume_ratio"] == pytest.approx(0.6, abs=0.01)
    assert ind["session_elapsed_pct"] == VERI_YOK


# ---------- tarama: BIST100 + sığ hisse filtresi ----------
def test_universe_defaults_to_bist100():
    from tools import screener
    u = screener.universe()
    assert len(u) >= 100 and "THYAO.IS" in u and "TRALT.IS" in u
    assert len(u) == len(set(u))


def test_screen_drops_illiquid_names(ohlcv):
    from tools import screener
    thin = ohlcv.copy()
    thin["volume"] = 1.0  # ~100 TL/gün işlem hacmi
    data = {"XU100.IS": ohlcv, "LIQ.IS": ohlcv, "THIN.IS": thin}
    rows = screener.screen(["LIQ.IS", "THIN.IS"], top_n=5, fetcher=lambda t, p: data[t])
    assert [r["ticker"] for r in rows] == ["LIQ.IS"]


# ---------- yerel ambar tazeliği ----------
def test_expected_last_bar():
    from datetime import date
    assert market_data.expected_last_bar(_tr(9, 30)) == date(2026, 11, 23)   # Salı açılış öncesi -> Pazartesi
    assert market_data.expected_last_bar(_tr(10, 30)) == date(2026, 11, 24)  # seans açık -> bugün
    from datetime import datetime
    sat = datetime(2026, 11, 28, 12, 0, tzinfo=market_data._TR)
    assert market_data.expected_last_bar(sat) == date(2026, 11, 27)          # Cumartesi -> Cuma


def test_warehouse_freshness_rules():
    df = _intraday_df(500.0)  # son mum 24.11.2026
    assert market_data._warehouse_is_fresh("THYAO.IS", df, _tr(10, 30))
    assert market_data._warehouse_is_fresh("THYAO.IS", df.iloc[:-1], _tr(9, 30))       # dünkü mum, seans kapalı
    assert not market_data._warehouse_is_fresh("THYAO.IS", df.iloc[:-1], _tr(10, 30))  # seans açık, bugün yok
    assert not market_data._warehouse_is_fresh("THYAO.IS", df.iloc[:-3], _tr(9, 30))   # birkaç gün eski
    assert not market_data._warehouse_is_fresh("^VIX", df, _tr(10, 30))                # küresel: hep canlı


def test_open_bar_not_written_to_warehouse():
    df = _intraday_df(500.0)
    assert len(market_data._without_open_bar(df, _tr(11, 0))) == len(df) - 1
    assert len(market_data._without_open_bar(df, _tr(18, 30))) == len(df)


def test_stale_warehouse_falls_back_to_live(monkeypatch):
    import sys, types
    from config.settings import settings
    stale = _intraday_df(500.0).iloc[:-3]
    live = _intraday_df(500.0).rename(columns=str.title)
    written = []

    class WH:
        def get_ohlcv(self, t, period="1y"):
            return stale

        def upsert_ohlcv(self, t, df):
            written.append(len(df))

    monkeypatch.setattr("tools.data_warehouse.get_warehouse", lambda: WH())
    fake_yf = types.SimpleNamespace(Ticker=lambda t: types.SimpleNamespace(history=lambda **k: live))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    object.__setattr__(settings, "use_local_warehouse", True)
    try:
        out = market_data.fetch_price_history("THYAO.IS", now=_tr(11, 0))
    finally:
        object.__setattr__(settings, "use_local_warehouse", False)
    assert out.index[-1].date().isoformat() == "2026-11-24"  # canlı veri döndü
    assert written == [len(live) - 1]                         # yarım mum ambara yazılmadı
