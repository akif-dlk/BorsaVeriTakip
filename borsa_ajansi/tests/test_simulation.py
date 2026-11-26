from datetime import date, timedelta

import pandas as pd
import pytest

from config.settings import settings
from simulation import paper_trader as pt
from tools import screener
from verification.schemas import FinalDecision, TradePlan


class FakeFeed:
    def __init__(self):
        self.prices = {"XU100.IS": 10000.0}
        self.bars: dict[str, pd.DataFrame] = {}

    def last(self, t):
        return self.prices.get(t)

    def daily(self, t, start):
        return self.bars.get(t, pd.DataFrame())


class Clock:
    def __init__(self, d):
        self.d = d

    def __call__(self):
        return self.d


def _final(ticker, decision="AL", stop=95.0, target=115.0, entry_high=100.5, conf=0.8, rr=2.5):
    plan = TradePlan(entry_low=98, entry_high=entry_high, stop_loss=stop, target=target, risk_reward=rr, basis="t") \
        if decision == "AL" else None
    return {"final": FinalDecision(ticker=ticker, data_date="2026-11-20", decision=decision, llm_decision=decision,
                                   raw_confidence=conf, confidence=conf, winning_side="boga", rationale="test",
                                   key_risks=[], trade_plan=plan, close=100.0)}


@pytest.fixture
def env(tmp_path):
    feed = FakeFeed()
    clock = Clock(date(2026, 11, 23))
    decisions = {}
    trader = pt.PaperTrader(str(tmp_path / "sim.db"), feed=feed, clock=clock,
                            analyze_fn=lambda t: decisions.get(t) or _final(t, "BEKLE"),
                            screen_fn=lambda exclude=None: [{"ticker": t, "score": 50} for t in ("AAA.IS", "BBB.IS")
                                                            if t not in (exclude or set())])
    return trader, feed, clock, decisions


def test_position_sizing_respects_risk_cap_and_cash():
    # 10k öz sermaye, %2 risk = 200 TL; risk/adet ~5.1 -> 39 adet; %30 tavan = 29 adet -> 29
    assert pt.size_position(10000, 10000, 100.0, 95.0) == 29
    # çok dar stop: risk sınırı yüksek, tavan belirler
    assert pt.size_position(10000, 10000, 100.0, 99.5) == 29
    # nakit sınırı
    assert pt.size_position(10000, 500, 100.0, 95.0) == 4
    # stop fiyatın üstünde -> 0
    assert pt.size_position(10000, 10000, 100.0, 101.0) == 0


def test_full_cycle_buy_target_stop_and_report(env):
    trader, feed, clock, decisions = env
    feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 50.0})
    run = trader.start(10000, 14)
    assert run["initial_cash"] == 10000 and run["end_date"] == "2026-12-07"
    with pytest.raises(RuntimeError):
        trader.start()

    decisions["AAA.IS"] = _final("AAA.IS", "AL", stop=95, target=110)
    decisions["BBB.IS"] = _final("BBB.IS", "AL", stop=47, target=60, entry_high=40)  # fiyat girişin üstünde
    msgs = trader.trade_cycle()
    assert any("ALIŞ AAA.IS" in m for m in msgs)
    assert any("BBB.IS" in m and "kovalanmadı" in m for m in msgs)
    opens = trader.open_trades(run["id"])
    assert len(opens) == 1 and opens[0]["qty"] == 29
    cash_after_buy = trader.active_run()["cash"]
    assert cash_after_buy == pytest.approx(10000 - 29 * 100.1 - round(29 * 100.1 * 0.002, 2), abs=0.01)

    # ertesi gün hedef görüldü
    clock.d = date(2026, 11, 24)
    feed.bars["AAA.IS"] = pd.DataFrame({"open": [101.0], "high": [111.0], "low": [100.5], "close": [110.5]},
                                       index=[date(2026, 11, 24)])
    msgs = trader.check_exits()
    assert any("hedef" in m for m in msgs)
    closed = trader.closed_trades(run["id"])
    assert closed[0]["exit_reason"] == "hedef" and closed[0]["pnl"] > 0
    assert closed[0]["exit_price"] == pytest.approx(110 * (1 - 0.001))

    rep = trader.report()
    assert "10,000 TL" in rep and "Kapanan işlemler: 1" in rep and "kazanan 1" in rep


def test_stop_has_priority_and_gap_down_fills_at_open(env):
    trader, feed, clock, decisions = env
    feed.prices.update({"AAA.IS": 100.0})
    run = trader.start(10000, 14)
    trader.buy(run, "AAA.IS", 100.0, 95.0, 110.0)
    clock.d = date(2026, 11, 24)
    # gap down: açılış 90 < stop 95, aynı gün hedef de görülüyor -> stop, açılıştan
    feed.bars["AAA.IS"] = pd.DataFrame({"open": [90.0], "high": [112.0], "low": [89.0], "close": [111.0]},
                                       index=[date(2026, 11, 24)])
    trader.check_exits()
    t = trader.closed_trades(run["id"])[0]
    assert t["exit_reason"] == "stop-loss"
    assert t["exit_price"] == pytest.approx(90 * 0.999)
    assert t["pnl"] < 0


def test_entry_day_bar_ignored(env):
    trader, feed, clock, _ = env
    feed.prices["AAA.IS"] = 100.0
    run = trader.start()
    trader.buy(run, "AAA.IS", 100.0, 95.0, 110.0)
    feed.bars["AAA.IS"] = pd.DataFrame({"open": [100.0], "high": [100.5], "low": [94.0], "close": [99.0]},
                                       index=[date(2026, 11, 23)])  # giriş günü
    assert not [m for m in trader.check_exits() if "SATIŞ" in m]


def test_sell_signal_closes_and_al_raises_stop(env):
    trader, feed, clock, decisions = env
    feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 100.0})
    run = trader.start()
    trader.buy(run, "AAA.IS", 100.0, 95.0, 110.0)
    trader.buy(run, "BBB.IS", 100.0, 95.0, 110.0)
    decisions["AAA.IS"] = _final("AAA.IS", "SAT")
    decisions["BBB.IS"] = _final("BBB.IS", "AL", stop=97.0)
    msgs = trader.trade_cycle()
    assert any("SAT sinyali" in m for m in msgs)
    assert any("stop yükseltildi" in m for m in msgs)
    bbb = [t for t in trader.open_trades(run["id"]) if t["ticker"] == "BBB.IS"][0]
    assert bbb["stop_loss"] == 97.0
    # daha düşük stop önerisi stop'u indirmez
    decisions["BBB.IS"] = _final("BBB.IS", "AL", stop=90.0)
    trader.trade_cycle()
    bbb = [t for t in trader.open_trades(run["id"]) if t["ticker"] == "BBB.IS"][0]
    assert bbb["stop_loss"] == 97.0


def test_expiry_closes_everything_and_reports_vs_benchmark(env):
    trader, feed, clock, _ = env
    feed.prices.update({"AAA.IS": 100.0})
    run = trader.start(10000, 14)
    trader.buy(run, "AAA.IS", 100.0, 95.0, 110.0)
    clock.d = date(2026, 12, 7)
    feed.prices.update({"AAA.IS": 104.0, "XU100.IS": 10100.0})
    msgs = trader.check_exits()
    assert any("süre doldu" in m for m in msgs)
    assert trader.active_run() is None
    assert trader.last_run()["status"] == "finished"
    rep = trader.report()
    assert "tamamlandı" in rep and "BIST100 aynı dönem: +1.00%" in rep


def test_stop_command(env):
    trader, feed, _, _ = env
    feed.prices["AAA.IS"] = 100.0
    run = trader.start()
    trader.buy(run, "AAA.IS", 100.0, 95.0, 110.0)
    assert trader.stop()["status"] == "stopped"
    assert trader.open_trades(run["id"]) == []


def test_max_positions(env, monkeypatch):
    trader, feed, _, decisions = env
    object.__setattr__(settings, "sim_max_positions", 1)
    try:
        feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 100.0})
        trader.start()
        decisions["AAA.IS"] = _final("AAA.IS")
        decisions["BBB.IS"] = _final("BBB.IS")
        trader.trade_cycle()
        assert len(trader.open_trades(trader.active_run()["id"])) == 1
    finally:
        object.__setattr__(settings, "sim_max_positions", 4)


# ---------------- kademeli yatırım + daha iyi adaya geçiş ----------------
def test_tier_limit_mapping():
    assert pt.tier_limit(0.66, 1.35) == 0.5
    assert pt.tier_limit(0.69, 1.45) == 0.75
    assert pt.tier_limit(0.72, 2.0) == 1.0
    assert pt.tier_limit(0.72, 1.35) == 0.5  # güven yüksek ama R/Ö yalnızca 1. kademeye yetiyor
    assert pt.tier_limit(0.60, 3.0) is None


def test_weak_signals_cannot_exceed_first_half(env):
    trader, feed, clock, decisions = env
    for t in ("AAA.IS", "BBB.IS", "CCC.IS"):
        feed.prices[t] = 100.0
    run = trader.start(10000, 14)
    # 1. kademe sinyalleri (0.66 / 1.35): portföyün en fazla %50'si
    trader.screen_fn = lambda exclude=None: [{"ticker": t, "score": 50} for t in ("AAA.IS", "BBB.IS", "CCC.IS")
                                             if t not in (exclude or set())]
    for t in ("AAA.IS", "BBB.IS", "CCC.IS"):
        decisions[t] = _final(t, conf=0.66, rr=1.35)
    msgs = trader.trade_cycle()
    eq, _ = trader.equity(run)
    invested = eq - trader.active_run()["cash"]
    assert invested <= 0.5 * eq + 1
    assert any("kademe limiti dolu" in m for m in msgs)


def test_strong_signal_uses_remaining_tiers(env):
    trader, feed, clock, decisions = env
    feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 100.0})
    run = trader.start(10000, 14)
    trader.buy(run, "AAA.IS", 100.0, 95.0, 115.0, 0.66, max_value=5000)  # ~%30
    trader.buy(run, "ZZZ.IS", 100.0, 95.0, 115.0, 0.66, max_value=2000)  # toplam ~%50
    feed.prices["ZZZ.IS"] = 100.0
    decisions["AAA.IS"] = decisions["ZZZ.IS"] = _final("x", "BEKLE", conf=0.66)
    decisions["BBB.IS"] = _final("BBB.IS", conf=0.75, rr=2.0)  # 3. kademe -> %100'e kadar
    msgs = trader.trade_cycle()
    assert any("ALIŞ BBB.IS" in m and "kademe ≤%100" in m for m in msgs)


def test_rotation_sells_weakest_for_much_better_candidate(env):
    trader, feed, clock, decisions = env
    object.__setattr__(settings, "sim_max_positions", 1)
    try:
        feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 100.0})
        run = trader.start(10000, 14)
        trader.buy(run, "AAA.IS", 100.0, 95.0, 107.0, 0.66)  # kalan R/Ö ~1.4
        clock.d = date(2026, 11, 24)
        decisions["AAA.IS"] = _final("AAA.IS", "BEKLE", conf=0.66)
        decisions["BBB.IS"] = _final("BBB.IS", conf=0.78, rr=2.5)
        msgs = trader.trade_cycle()
        assert any("SATIŞ AAA.IS" in m and "daha iyi adaya geçiş" in m for m in msgs)
        assert [t["ticker"] for t in trader.open_trades(run["id"])] == ["BBB.IS"]
    finally:
        object.__setattr__(settings, "sim_max_positions", 4)


def test_no_rotation_when_only_slightly_better(env):
    trader, feed, clock, decisions = env
    object.__setattr__(settings, "sim_max_positions", 1)
    try:
        feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 100.0})
        run = trader.start(10000, 14)
        trader.buy(run, "AAA.IS", 100.0, 95.0, 110.0, 0.70)  # kalan R/Ö 2.0
        clock.d = date(2026, 11, 24)
        decisions["AAA.IS"] = _final("AAA.IS", "BEKLE", conf=0.70)
        decisions["BBB.IS"] = _final("BBB.IS", conf=0.73, rr=2.2)  # +0.03 güven: yetersiz
        msgs = trader.trade_cycle()
        assert not any("daha iyi adaya geçiş" in m for m in msgs)
        assert [t["ticker"] for t in trader.open_trades(run["id"])] == ["AAA.IS"]
    finally:
        object.__setattr__(settings, "sim_max_positions", 4)


def test_position_bought_today_is_not_rotated(env):
    trader, feed, clock, decisions = env
    object.__setattr__(settings, "sim_max_positions", 1)
    try:
        feed.prices.update({"AAA.IS": 100.0, "BBB.IS": 100.0})
        run = trader.start(10000, 14)
        trader.buy(run, "AAA.IS", 100.0, 95.0, 107.0, 0.66)
        decisions["AAA.IS"] = _final("AAA.IS", "BEKLE", conf=0.66)
        decisions["BBB.IS"] = _final("BBB.IS", conf=0.9, rr=3.0)
        msgs = trader.trade_cycle()
        assert not any("daha iyi adaya geçiş" in m for m in msgs)
    finally:
        object.__setattr__(settings, "sim_max_positions", 4)


# ---------------- screener ----------------
def test_screener_ranks_uptrend_over_downtrend(ohlcv):
    up = ohlcv
    down = ohlcv.copy()
    down[["open", "high", "low", "close"]] = down[["open", "high", "low", "close"]].iloc[::-1].values
    flat = ohlcv.copy()
    data = {"XU100.IS": flat, "UP.IS": up, "DOWN.IS": down}
    rows = screener.screen(["UP.IS", "DOWN.IS"], top_n=2, fetcher=lambda t, p: data[t])
    assert rows[0]["ticker"] == "UP.IS"
    assert screener.screen(["UP.IS"], top_n=2, fetcher=lambda t, p: data[t], exclude={"UP.IS"}) == []


def test_screener_skips_missing_data(ohlcv):
    rows = screener.screen(["X.IS"], fetcher=lambda t, p: ohlcv if t == "XU100.IS" else pd.DataFrame())
    assert rows == []
