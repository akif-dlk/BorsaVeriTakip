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
