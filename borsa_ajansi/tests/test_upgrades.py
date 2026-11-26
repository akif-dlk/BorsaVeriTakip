"""Yeni eklenen modüllerin birim testleri (ağ ve gerçek LLM gerektirmez):
- Yerel veri ambarı (`tools/data_warehouse.py`)
- Sıfır-LLM deterministik KAP ve faaliyet raporu ayrıştırıcı (`agents/document_reader.py`)
- Tek-çağrılı ekonomik analist modu (`agents/analysts.py`) ve analiz önbelleği (`graph/workflow.py`)
- İki kanallı tarama + R/Ö ön filtresi (`tools/screener.py`)
- BIST sektörel çarpan kıyası (`tools/fundamental_data.py`) ve BIST rejimi (`tools/market_context.py`)
- Risk Yöneticisi aşırı-BEKLE kalibrasyonu (`agents/risk_manager.py`)
- Alfa bazlı tahmin karnesi + kalibrasyon özeti (`memory/db.py`)
- PPF nakit getirisi + 1.5R başabaş stop + deterministik backtest motoru (`simulation/backtest.py`)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from agents import analysts, document_reader as dr, risk_manager
from config.settings import settings
from graph.workflow import run_analysis
from memory.cache import DocCache
from memory.db import PredictionStore, judge_alpha
from simulation import paper_trader as pt
from simulation.backtest import BacktestConfig, format_backtest_report, run_backtest
from tests.fakes import FakeLLM, make_bundle
from tests.test_documents import fake_get, pdf  # noqa: F401
from tests.test_simulation import Clock, FakeFeed
from tests.test_workflow import Script
from tools import data_warehouse as dw, fundamental_data, market_context, screener
from verification.schemas import (
    AnalystReport,
    BatchAnalystReports,
    FinalDecision,
    NumericClaim,
    RiskJudgement,
    TradePlan,
)


# ---------------- 1) Yerel Veri Ambarı ----------------
def test_warehouse_sync_incremental_and_csv(tmp_path, ohlcv):
    wh = dw.DataWarehouse(str(tmp_path / "wh.db"), str(tmp_path / "bist.csv"))
    calls = []

    def fake_dl(symbols, start):
        calls.append((list(symbols), start))
        return {s: ohlcv for s in symbols}

    last_bar_day = ohlcv.index[-1].date()
    res1 = wh.sync_market_data(tickers=["THYAO.IS", "XU100.IS"], years=2, downloader=fake_dl, today=last_bar_day)
    assert res1["symbols"] == 2 and res1["total_rows"] == len(ohlcv) * 2
    assert (tmp_path / "bist.csv").exists()

    # İkinci senkronizasyon: veri zaten dolu olduğu için yalnızca son birkaç günlük artımlı fark istenir
    res2 = wh.sync_market_data(tickers=["THYAO.IS", "XU100.IS"], years=2, downloader=fake_dl, today=last_bar_day)
    assert len(calls) == 2
    assert calls[1][1] >= last_bar_day - timedelta(days=6)  # 2 yıllık tam indirme tekrarlanmadı
    assert res2["total_rows"] == len(ohlcv) * 2

    got = wh.get_ohlcv("THYAO.IS", period="1y", today=last_bar_day)
    assert not got.empty and "close" in got.columns
    # Çok eski tarih verilirse bayat sayılır -> boş döner
    stale = wh.get_ohlcv("THYAO.IS", period="1y", today=last_bar_day + timedelta(days=20))
    assert stale.empty

    hmap = wh.load_history_map(["THYAO.IS", "XU100.IS"])
    assert set(hmap) == {"THYAO.IS", "XU100.IS"} and len(hmap["THYAO.IS"]) == len(ohlcv)


def test_warehouse_kap_sync_zero_llm(tmp_path):
    wh = dw.DataWarehouse(str(tmp_path / "wh.db"), str(tmp_path / "bist.csv"))
    res = wh.sync_kap_disclosures(tickers=["ACME.IS"], lookback_days=3650, get=fake_get)
    assert res["symbols_checked"] == 1 and res["disclosures_saved"] >= 2
    cats = wh.recent_kap_catalysts(days=3650, today=date(2026, 11, 25))
    assert "ACME.IS" in cats


# ---------------- 2) Sıfır-LLM Belge Ayrıştırıcı & Ekonomik Mod ----------------
def test_deterministic_documents_zero_llm_calls(tmp_path, pdf):
    def must_not_call(schema, system, user):
        raise AssertionError("LLM çağrılmamalıydı!")

    llm = FakeLLM(must_not_call)
    cache = DocCache(str(tmp_path / "doc.db"))
    out = dr.get_documents(llm, "ACME.IS", get=fake_get, cache=cache, today=datetime(2026, 11, 24), use_llm=False)
    assert len(llm.calls) == 0
    assert out["kap"]["status"] == "ok" and out["kap"]["source"] == "KAP_deterministic"
    assert any(n["impact"] == "olumlu" and n["materiality"] == "yuksek" for n in out["kap"]["notes"])
    assert out["outlook"]["status"] == "ok" and len(out["outlook"]["items"]) >= 1
    assert any("2027" in it["horizon"] or "2028" in it["horizon"] for it in out["outlook"]["items"])


def test_economy_single_call_reduces_analyst_calls_from_4_to_1(ohlcv):
    b = make_bundle(ohlcv)
    d_str = b["market"]["indicators"]["date"]
    rsi = b["market"]["indicators"]["rsi14"]

    def batch_responder(schema, system, user):
        assert schema is BatchAnalystReports
        mk = lambda name, f, v: AnalystReport(
            analyst=name, ticker="TEST.IS", data_date=d_str, signal="olumlu",
            score=0.5, confidence=0.8, key_points=["net"], claims=[NumericClaim(field=f, value=v)],
            data_gaps=[], summary="özet",
        )
        return BatchAnalystReports(
            teknik=mk("teknik", "rsi14", rsi),
            temel=mk("temel", "pe_ratio", 6.2),
            gundem=mk("gundem", "news_count", 3),
            duygu=mk("duygu", "rsi14", rsi),
        )

    llm = FakeLLM(batch_responder)
    object.__setattr__(settings, "economy_single_call", True)
    try:
        reps = analysts.run_analysts_parallel(llm, list(analysts.ANALYSTS), "TEST.IS", b)
        assert len(llm.calls) == 1
        assert set(reps) == {"teknik", "temel", "gundem", "duygu"}
    finally:
        object.__setattr__(settings, "economy_single_call", False)


# ---------------- 3) Sektörel Çarpan Kıyası, BIST Rejimi ve İki Kanallı Tarama ----------------
def test_bist_sector_comparison_discount_and_premium():
    # ASELS savunma_teknoloji medyanı F/K=18.5, PD/DD=4.20 -> F/K=10, PD/DD=2.0 iskontolu olmalı
    f_cheap = fundamental_data.get_fundamentals(
        "ASELS", info_fetcher=lambda t: {"trailingPE": 10.0, "priceToBook": 2.0}, mcp_fetcher=lambda t: {}
    )
    assert f_cheap["sector_comparison"]["bist_sector_group"] == "savunma_teknoloji"
    assert f_cheap["sector_comparison"]["valuation_vs_sector"] == "iskontolu"

    # GARAN banka medyanı F/K=4.8, PD/DD=1.25 -> F/K=9.0, PD/DD=2.5 primli olmalı
    f_rich = fundamental_data.get_fundamentals(
        "GARAN", info_fetcher=lambda t: {"trailingPE": 9.0, "priceToBook": 2.5}, mcp_fetcher=lambda t: {}
    )
    assert f_rich["sector_comparison"]["bist_sector_group"] == "banka"
    assert f_rich["sector_comparison"]["valuation_vs_sector"] == "primli"


def test_bist_regime_detection():
    ctx_def = {
        "bist100": {"status": "ok", "above_ma50": False, "change_5d_pct": -3.5},
        "bist_banka": {"status": "ok", "change_5d_pct": -5.0},
        "global_risk_regime": "risk_off",
    }
    r1 = market_context.compute_bist_regime(ctx_def)
    assert r1["bist_regime"] == "defansif" and r1["recommended_risk_mult"] == 0.5

    ctx_bull = {
        "bist100": {"status": "ok", "above_ma50": True, "change_5d_pct": 2.5},
        "bist_banka": {"status": "ok", "change_5d_pct": 3.0},
        "global_risk_regime": "risk_on",
    }
    r2 = market_context.compute_bist_regime(ctx_bull)
    assert r2["bist_regime"] == "boga" and r2["recommended_risk_mult"] == 1.0


def test_dual_channel_screener_includes_oversold_kap_catalyst(ohlcv):
    # Sert düşüş sonrası son gün hacimli tepki veren aşırı satım hissesi + olumlu KAP bildirimi
    n = 120
    idx = pd.bdate_range("2026-05-01", periods=n)
    close = np.concatenate([np.linspace(120, 80, n - 1), [81.5]])
    df_rev = pd.DataFrame(
        {
            "open": close - 0.3,
            "high": close + 1.5,
            "low": close - 1.0,
            "close": close,
            "volume": [2_000_000.0] * (n - 1) + [4_500_000.0],
        },
        index=idx,
    )
    data = {"XU100.IS": ohlcv, "MOM1.IS": ohlcv, "MOM2.IS": ohlcv, "REV1.IS": df_rev}
    kap_cat = {"REV1.IS": [{"title": "Yeni İhale", "summary": "500 milyon TL sözleşme imzalanmıştır", "body_text": ""}]}
    rows = screener.screen(
        ["MOM1.IS", "MOM2.IS", "REV1.IS"],
        top_n=3,
        fetcher=lambda t, p: data[t],
        include_reversals=True,
        prefilter_rr=False,
        kap_catalysts=kap_cat,
    )
    channels = {r["ticker"]: r["channel"] for r in rows}
    assert "REV1.IS" in channels and channels["REV1.IS"] == "donus_katalizor"


# ---------------- 4) Hakem Aşırı-BEKLE Kalibrasyonu ----------------
def test_risk_manager_promotes_timid_bekle_when_bull_conviction_strong(ohlcv):
    b = make_bundle(ohlcv)
    ind = b["market"]["indicators"]
    ind["levels"]["resistance_60d"] = round(ind["close"] + 8 * ind["atr14"], 4)
    reps = {
        "teknik": AnalystReport(analyst="teknik", ticker="TEST.IS", data_date=ind["date"], signal="olumlu",
                                score=0.7, confidence=0.8, key_points=["güçlü"], claims=[], data_gaps=[], summary="ok"),
        "temel": AnalystReport(analyst="temel", ticker="TEST.IS", data_date=ind["date"], signal="olumlu",
                               score=0.5, confidence=0.75, key_points=["ucuz"], claims=[], data_gaps=[], summary="ok"),
    }
    timid_j = RiskJudgement(
        decision="BEKLE",
        confidence=0.75,
        conviction_score=0.45,
        winning_side="boga",
        technical_fundamental_conflict=False,
        rationale="Boğa tezi güçlü ama genel ihtiyat",
        key_risks=["VIX"],
    )
    final = risk_manager.apply_gates("TEST.IS", b, reps, timid_j, 0.0, [])
    assert final.llm_decision == "BEKLE"
    assert final.decision == "AL"
    assert final.trade_plan is not None
    assert any("kalibrasyon" in g for g in final.gate_reasons)


# ---------------- 5) Alfa Karnesi, PPF, Başabaş Stop ve 3 Yıllık Backtest ----------------
def test_alpha_judge_and_calibration_summary(tmp_path):
    assert judge_alpha("AL", 3.0, 1.0) == 1      # +%3 getiri, endeks +%1 -> net alfa var
    assert judge_alpha("AL", 1.5, 4.0) == 0      # pozitif ama endeksin gerisinde -> alfa başarısız
    assert judge_alpha("AL", 0.3, -1.0) == 0     # komisyonu bile kurtarmıyor -> başarısız

    store = PredictionStore(str(tmp_path / "p.db"))
    for i in range(6):
        store.save_prediction(
            FinalDecision(
                ticker="THYAO.IS", data_date="2026-11-20", decision="BEKLE", llm_decision="BEKLE",
                raw_confidence=0.65, confidence=0.65, winning_side="berabere", rationale="r", key_risks=[],
            )
        )
    cal = store.calibration_summary()
    assert cal is not None and cal["bekle_ratio_pct"] == 100.0
    assert "aşırı çekimser" in cal["note"]


def test_ppf_yield_and_breakeven_stop(tmp_path):
    feed = FakeFeed()
    clock = Clock(date(2026, 11, 23))  # Pazartesi
    trader = pt.PaperTrader(str(tmp_path / "s.db"), feed=feed, clock=clock)
    feed.prices["AAA.IS"] = 100.0
    object.__setattr__(settings, "sim_cash_daily_yield_pct", 0.10)
    object.__setattr__(settings, "sim_breakeven_r", 1.5)
    try:
        run = trader.start(10000, 14)
        trader.buy(run, "AAA.IS", 100.0, 95.0, 120.0)  # risk ~5 TL -> 1.5R = ~107.65
        cash_before = trader.active_run()["cash"]
        clock.d = date(2026, 11, 24)  # Salı (1 iş günü)
        feed.bars["AAA.IS"] = pd.DataFrame(
            {"open": [101.0], "high": [109.0], "low": [100.5], "close": [108.0]},
            index=[date(2026, 11, 24)],
        )
        msgs = trader.check_exits()
        assert any("başabaşa çekildi" in m for m in msgs)
        assert trader.active_run()["cash"] > cash_before  # PPF işledi
        t = trader.open_trades(run["id"])[0]
        assert t["stop_loss"] > t["entry_price"]  # stop giriş+komisyon üstüne çıktı
    finally:
        object.__setattr__(settings, "sim_cash_daily_yield_pct", 0.0)
        object.__setattr__(settings, "sim_breakeven_r", 0.0)


def test_deterministic_backtester_runs_without_llm(ohlcv):
    rng = np.random.default_rng(7)
    n = len(ohlcv)
    idx = ohlcv.index
    c2 = 50 + np.cumsum(rng.normal(0.18, 1.1, n))
    df2 = pd.DataFrame(
        {"open": c2, "high": c2 + 1.5, "low": c2 - 1.2, "close": c2, "volume": np.full(n, 3_000_000.0)},
        index=idx,
    )
    hmap = {"XU100.IS": ohlcv, "AAA.IS": ohlcv, "BBB.IS": df2}
    res = run_backtest(
        history_map=hmap,
        days=150,
        cfg=BacktestConfig(initial_cash=100_000, min_score=30.0, min_rr=1.2, ppf_daily_pct=0.08),
    )
    assert res.trading_days == 150
    assert res.final_equity > 0
    assert res.ppf_earned_tl > 0
    txt = format_backtest_report(res)
    assert "Deterministik BIST Backtest Karnesi" in txt and "Sharpe Oranı" in txt
