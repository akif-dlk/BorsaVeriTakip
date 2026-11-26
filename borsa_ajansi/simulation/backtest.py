"""Deterministik 3 Yıllık BIST100 Backtest Motoru (0 LLM Çağrısı).

Amaç:
- Yerel veri ambarındaki (`data/market_warehouse.db` / `bist100_3y_daily.csv`) 2-3 yıllık
  günlük OHLCV verisi üzerinde stratejinin saf matematiksel beklentisini (Expectancy, Sharpe,
  Max Drawdown, Profit Factor, BIST100 ve PPF'ye karşı Alfa) saniyeler içinde ölçmek.
- İleriye bakma hatası (lookahead bias) yoktur: t gününün sinyali yalnızca <= t verisiyle üretilir;
  çıkış kontrolleri t+1 gününden itibaren günlük (open, high, low, close) barlarıyla yapılır.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from agents.risk_manager import compute_trade_plan
from config.settings import settings
from simulation.paper_trader import BENCHMARK, size_position
from tools.screener import score_indicators, score_reversal_catalyst


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    max_positions: int = 4
    candidates_per_day: int = 2
    min_score: float = 65.0
    min_rr: float = 1.6
    min_turnover_mtl: float = 75.0
    risk_per_trade_pct: float = 1.5
    max_position_pct: float = 25.0
    commission_pct: float = 0.2
    slippage_pct: float = 0.1
    ppf_daily_pct: float = 0.10      # Boştaki nakit için iş günü başı PPF getirisi (~%28-30 yıllık basit, ~%33 bileşik)
    breakeven_r: float = 1.4         # 1.4R kârda stop başabaşa çekilir
    trail_atr_mult: float = 2.0      # Kârdaki pozisyonda iz süren stop (close - 2.0*ATR)
    max_hold_days: int = 18          # Maksimum taşıma süresi (iş günü)
    include_reversals: bool = True   # Aşırı satım destek dönüş kanalını dahil et
    regime_filter: bool = True       # XU100 < MA50 iken kırılım kovalamayı durdur, PPF + dip dönüşte kal


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    trading_days: int
    symbols_tested: int
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    benchmark_return_pct: float
    alpha_vs_bist_pct: float
    ppf_only_return_pct: float
    ppf_earned_tl: float
    trading_return_pct: float  # yalnızca hisse işlemlerinin net K/Z'si (nakit faizi hariç)
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    total_trades: int
    win_rate_pct: float
    profit_factor: float
    expectancy_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_hold_days: float
    exit_reasons: dict[str, int] = field(default_factory=dict)
    channel_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    top_winners: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)


def _precompute_panel(df: pd.DataFrame) -> pd.DataFrame:
    """Tek bir hisse için tüm indikatör serilerini vektörel olarak tek seferde hesaplar."""
    if df is None or len(df) < 65:
        return pd.DataFrame()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)
    out["open"] = df["open"]
    out["high"] = h
    out["low"] = l
    out["close"] = c
    out["volume"] = v
    out["ma20"] = SMAIndicator(c, 20).sma_indicator()
    out["ma50"] = SMAIndicator(c, 50).sma_indicator()
    out["ma200"] = SMAIndicator(c, 200).sma_indicator() if len(df) >= 200 else np.nan
    out["rsi14"] = RSIIndicator(c, 14).rsi()
    macd = MACD(c)
    out["macd_hist"] = macd.macd_diff()
    out["atr14"] = AverageTrueRange(h, l, c, 14).average_true_range()
    bb = BollingerBands(c, 20)
    out["bb_upper"] = bb.bollinger_hband()
    out["bb_lower"] = bb.bollinger_lband()
    out["stoch_k"] = StochasticOscillator(h, l, c).stoch()
    out["avg_vol20"] = v.shift(1).rolling(20).mean()
    out["vol_ratio"] = v / out["avg_vol20"].replace(0, np.nan)
    out["ch1"] = c.pct_change(1) * 100
    out["ch5"] = c.pct_change(5) * 100
    out["ch20"] = c.pct_change(20) * 100
    out["sup20"] = l.rolling(20).min()
    out["res20"] = h.rolling(20).max()
    out["sup60"] = l.rolling(60).min()
    out["res60"] = h.rolling(60).max()
    return out


def _row_to_ind(r: pd.Series) -> dict[str, Any]:
    def cl(val):
        return "veri_yok" if pd.isna(val) else float(val)

    c, ma50 = cl(r["close"]), cl(r["ma50"])
    return {
        "status": "ok",
        "close": c,
        "ma20": cl(r["ma20"]),
        "ma50": ma50,
        "ma200": cl(r["ma200"]),
        "above_ma50": (c > ma50) if isinstance(c, float) and isinstance(ma50, float) else "veri_yok",
        "rsi14": cl(r["rsi14"]),
        "macd_hist": cl(r["macd_hist"]),
        "atr14": cl(r["atr14"]),
        "bb_upper": cl(r["bb_upper"]),
        "bb_lower": cl(r["bb_lower"]),
        "stoch_k": cl(r["stoch_k"]),
        "avg_volume_20d": cl(r["avg_vol20"]),
        "volume_ratio": cl(r["vol_ratio"]),
        "change_1d_pct": cl(r["ch1"]),
        "change_5d_pct": cl(r["ch5"]),
        "change_20d_pct": cl(r["ch20"]),
        "levels": {
            "support_20d": cl(r["sup20"]),
            "resistance_20d": cl(r["res20"]),
            "support_60d": cl(r["sup60"]),
            "resistance_60d": cl(r["res60"]),
        },
    }


def run_backtest(
    history_map: dict[str, pd.DataFrame] | None = None,
    days: int = 750,
    cfg: BacktestConfig | None = None,
) -> BacktestResult:
    """Yerel veri setinde (veya verilen OHLCV sözlüğünde) 0-LLM tarihsel portföy simülasyonu çalıştırır."""
    cfg = cfg or BacktestConfig()
    if history_map is None:
        from tools.data_warehouse import get_warehouse
        from tools.screener import universe

        wh = get_warehouse()
        syms = [BENCHMARK] + universe()
        history_map = wh.load_history_map(tickers=syms)

    if BENCHMARK not in history_map or history_map[BENCHMARK].empty:
        raise RuntimeError("Backtest için XU100.IS verisi bulunamadı. Önce `python main.py veri-cek` çalıştırın.")

    panels: dict[str, pd.DataFrame] = {}
    for tk, df in history_map.items():
        p = _precompute_panel(df)
        if not p.empty:
            panels[tk] = p

    bist_panel = panels[BENCHMARK]
    all_dates = bist_panel.index.sort_values()
    # İndikatör ısınma payı (MA200 / 60g destek) sonrası son `days` işlem günü
    sim_dates = all_dates[-days:] if len(all_dates) > days else all_dates[60:]
    if len(sim_dates) < 10:
        raise RuntimeError("Backtest için yeterli işlem günü yok (en az 70 bar gerekli).")

    stock_tickers = [t for t in panels if t != BENCHMARK and t.endswith(".IS")]
    cash = cfg.initial_cash
    ppf_earned = 0.0
    open_pos: dict[str, dict[str, Any]] = {}
    closed_trades: list[dict[str, Any]] = []
    eq_rows: list[dict[str, Any]] = []
    min_turnover = cfg.min_turnover_mtl * 1e6

    old_comm, old_slip, old_risk, old_cap = (
        settings.sim_commission_pct,
        settings.sim_slippage_pct,
        settings.sim_risk_per_trade_pct,
        settings.sim_max_position_pct,
    )
    object.__setattr__(settings, "sim_commission_pct", cfg.commission_pct)
    object.__setattr__(settings, "sim_slippage_pct", cfg.slippage_pct)
    object.__setattr__(settings, "sim_risk_per_trade_pct", cfg.risk_per_trade_pct)
    object.__setattr__(settings, "sim_max_position_pct", cfg.max_position_pct)

    try:
        for dt in sim_dates:
            # 1) Boştaki nakde günlük PPF getirisi
            if cfg.ppf_daily_pct > 0 and cash > 0:
                gain = cash * (cfg.ppf_daily_pct / 100.0)
                cash += gain
                ppf_earned += gain

            # 2) Açık pozisyon çıkış ve stop güncelleme kontrolleri
            for tk in list(open_pos.keys()):
                pos = open_pos[tk]
                p_df = panels[tk]
                if dt not in p_df.index:
                    continue
                bar = p_df.loc[dt]
                pos["hold_days"] += 1
                exit_px, reason = None, None

                if bar["low"] <= pos["stop"]:
                    exit_px = min(float(bar["open"]), pos["stop"])
                    reason = "stop-loss" if pos["stop"] <= pos["initial_stop"] + 1e-6 else "iz_suren_stop"
                elif bar["high"] >= pos["target"]:
                    exit_px = max(float(bar["open"]), pos["target"])
                    reason = "hedef"
                elif pos["hold_days"] >= cfg.max_hold_days:
                    exit_px = float(bar["close"])
                    reason = "zaman_stopu"
                elif (
                    pos["hold_days"] >= 8
                    and pd.notna(bar["ma20"])
                    and bar["close"] < bar["ma20"]
                    and pd.notna(bar["rsi14"])
                    and bar["rsi14"] < 44
                ):
                    exit_px = float(bar["close"])
                    reason = "trend_zayiflama"

                if exit_px is not None:
                    fill = exit_px * (1 - cfg.slippage_pct / 100.0)
                    comm = fill * pos["qty"] * (cfg.commission_pct / 100.0)
                    proceeds = fill * pos["qty"] - comm
                    cost = pos["entry_fill"] * pos["qty"] + pos["entry_comm"]
                    pnl = proceeds - cost
                    cash += proceeds
                    closed_trades.append(
                        {
                            "ticker": tk,
                            "channel": pos["channel"],
                            "entry_date": str(pos["entry_date"].date()),
                            "exit_date": str(dt.date()),
                            "hold_days": pos["hold_days"],
                            "pnl": pnl,
                            "pnl_pct": (pnl / cost) * 100.0,
                            "reason": reason,
                        }
                    )
                    del open_pos[tk]
                    continue

                # Başabaş (1.5R) ve ATR iz süren stop yükseltme
                init_risk = max(0.01, pos["entry_fill"] - pos["initial_stop"])
                if cfg.breakeven_r > 0 and pos["stop"] < pos["entry_fill"]:
                    if bar["high"] >= pos["entry_fill"] + cfg.breakeven_r * init_risk:
                        pos["stop"] = round(pos["entry_fill"] * (1 + 2 * cfg.commission_pct / 100.0), 4)
                if pd.notna(bar["atr14"]) and bar["close"] > pos["entry_fill"] + init_risk:
                    trail = float(bar["close"] - cfg.trail_atr_mult * bar["atr14"])
                    if trail > pos["stop"]:
                        pos["stop"] = round(trail, 4)

            # 3) Portföy anlık öz sermayesi ve piyasa rejimi
            pos_val = sum(
                float(panels[tk].loc[dt, "close"]) * p["qty"]
                if dt in panels[tk].index
                else p["last_px"] * p["qty"]
                for tk, p in open_pos.items()
            )
            for tk, p in open_pos.items():
                if dt in panels[tk].index:
                    p["last_px"] = float(panels[tk].loc[dt, "close"])
            equity = cash + pos_val

            b_row = bist_panel.loc[dt]
            b20 = float(b_row["ch20"]) if pd.notna(b_row["ch20"]) else None
            defensive = False
            if cfg.regime_filter and pd.notna(b_row["ma50"]):
                # BIST100 50 günlük ortalamasının altındayken veya 20g momentumu negatifken testere/düşüş rejimidir
                defensive = bool(
                    b_row["close"] < b_row["ma50"]
                    or (pd.notna(b_row["ch20"]) and b_row["ch20"] < 0)
                )

            eff_max_pos = max(1, cfg.max_positions // 2) if defensive else cfg.max_positions
            risk_mult = 0.6 if defensive else 1.0

            # 4) Yeni aday taraması ve giriş (yalnızca boş yer varsa)
            if len(open_pos) < eff_max_pos:
                mom_cands: list[tuple[float, str, dict, Any, str]] = []
                rev_cands: list[tuple[float, str, dict, Any, str]] = []
                for tk in stock_tickers:
                    if tk in open_pos or dt not in panels[tk].index:
                        continue
                    r = panels[tk].loc[dt]
                    if pd.isna(r["close"]) or pd.isna(r["atr14"]) or r["atr14"] <= 0:
                        continue
                    if min_turnover > 0 and pd.notna(r["avg_vol20"]) and (r["close"] * r["avg_vol20"] < min_turnover):
                        continue
                    ind = _row_to_ind(r)
                    plan = compute_trade_plan("AL", ind)
                    if plan is None or plan.risk_reward < cfg.min_rr:
                        continue
                    if not defensive:
                        sc, _ = score_indicators(ind, b20)
                        if sc >= cfg.min_score:
                            mom_cands.append((sc, tk, ind, plan, "trend_momentum"))
                    if cfg.include_reversals:
                        rsc, _ = score_reversal_catalyst(ind)
                        if rsc >= 40.0:
                            rev_cands.append((rsc, tk, ind, plan, "donus_katalizor"))

                mom_cands.sort(key=lambda x: x[0], reverse=True)
                rev_cands.sort(key=lambda x: x[0], reverse=True)
                merged = mom_cands[: max(1, cfg.candidates_per_day - 1)] + rev_cands[:1] + mom_cands[max(1, cfg.candidates_per_day - 1) :]
                seen_tk: set[str] = set()
                for sc, tk, ind, plan, ch in merged:
                    if tk in seen_tk or len(open_pos) >= eff_max_pos:
                        continue
                    seen_tk.add(tk)
                    px = float(ind["close"])
                    qty = size_position(equity, cash, px, plan.stop_loss, risk_mult=risk_mult)
                    if qty < 1:
                        continue
                    fill = px * (1 + cfg.slippage_pct / 100.0)
                    comm = fill * qty * (cfg.commission_pct / 100.0)
                    if fill * qty + comm > cash:
                        continue
                    cash -= fill * qty + comm
                    open_pos[tk] = {
                        "qty": qty,
                        "entry_fill": fill,
                        "entry_comm": comm,
                        "entry_date": dt,
                        "stop": plan.stop_loss,
                        "initial_stop": plan.stop_loss,
                        "target": plan.target,
                        "hold_days": 0,
                        "last_px": px,
                        "channel": ch,
                    }

            pos_val_end = sum(p["last_px"] * p["qty"] for p in open_pos.values())
            eq_rows.append(
                {
                    "date": dt,
                    "equity": cash + pos_val_end,
                    "cash": cash,
                    "bench": float(b_row["close"]),
                }
            )

        # Dönem sonunda açık kalan pozisyonları son kapanıştan kapat
        last_dt = sim_dates[-1]
        for tk, pos in list(open_pos.items()):
            fill = pos["last_px"] * (1 - cfg.slippage_pct / 100.0)
            comm = fill * pos["qty"] * (cfg.commission_pct / 100.0)
            proceeds = fill * pos["qty"] - comm
            cost = pos["entry_fill"] * pos["qty"] + pos["entry_comm"]
            pnl = proceeds - cost
            cash += proceeds
            closed_trades.append(
                {
                    "ticker": tk,
                    "channel": pos["channel"],
                    "entry_date": str(pos["entry_date"].date()),
                    "exit_date": str(last_dt.date()),
                    "hold_days": pos["hold_days"],
                    "pnl": pnl,
                    "pnl_pct": (pnl / cost) * 100.0,
                    "reason": "donem_sonu",
                }
            )
        open_pos.clear()
        if eq_rows:
            eq_rows[-1]["equity"] = cash
            eq_rows[-1]["cash"] = cash
    finally:
        object.__setattr__(settings, "sim_commission_pct", old_comm)
        object.__setattr__(settings, "sim_slippage_pct", old_slip)
        object.__setattr__(settings, "sim_risk_per_trade_pct", old_risk)
        object.__setattr__(settings, "sim_max_position_pct", old_cap)

    eq_df = pd.DataFrame(eq_rows).set_index("date")
    final_eq = float(eq_df["equity"].iloc[-1])
    tot_ret = (final_eq / cfg.initial_cash - 1.0) * 100.0
    bench_ret = (float(eq_df["bench"].iloc[-1]) / float(eq_df["bench"].iloc[0]) - 1.0) * 100.0
    n_days = len(eq_df)
    years_elapsed = max(0.1, n_days / 252.0)
    cagr = ((final_eq / cfg.initial_cash) ** (1.0 / years_elapsed) - 1.0) * 100.0
    ppf_only = (((1.0 + cfg.ppf_daily_pct / 100.0) ** n_days) - 1.0) * 100.0 if cfg.ppf_daily_pct > 0 else 0.0

    roll_max = eq_df["equity"].cummax()
    dd = (eq_df["equity"] / roll_max - 1.0) * 100.0
    max_dd = float(dd.min())

    daily_ret = eq_df["equity"].pct_change().dropna()
    rf_daily = cfg.ppf_daily_pct / 100.0
    excess = daily_ret - rf_daily
    sharpe = float(excess.mean() / daily_ret.std() * math.sqrt(252)) if len(daily_ret) > 2 and daily_ret.std() > 0 else 0.0
    downside = daily_ret[daily_ret < rf_daily] - rf_daily
    down_std = float(np.sqrt((downside ** 2).mean())) if len(downside) > 0 else 0.0
    sortino = float(excess.mean() / down_std * math.sqrt(252)) if down_std > 0 else 0.0

    n_trades = len(closed_trades)
    wins = [t for t in closed_trades if t["pnl"] > 0]
    losses = [t for t in closed_trades if t["pnl"] <= 0]
    win_rate = (len(wins) / n_trades * 100.0) if n_trades else 0.0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    expectancy = float(np.mean([t["pnl_pct"] for t in closed_trades])) if closed_trades else 0.0
    avg_win = float(np.mean([t["pnl_pct"] for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t["pnl_pct"] for t in losses])) if losses else 0.0
    avg_hold = float(np.mean([t["hold_days"] for t in closed_trades])) if closed_trades else 0.0

    reasons: dict[str, int] = {}
    ch_stats: dict[str, dict[str, float]] = {}
    for t in closed_trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    for ch in ("trend_momentum", "donus_katalizor"):
        sub = [t for t in closed_trades if t["channel"] == ch]
        if sub:
            ch_stats[ch] = {
                "trades": len(sub),
                "win_rate_pct": round(sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100, 1),
                "avg_pnl_pct": round(float(np.mean([t["pnl_pct"] for t in sub])), 2),
            }

    top_w = sorted(closed_trades, key=lambda x: x["pnl_pct"], reverse=True)[:5]
    return BacktestResult(
        start_date=str(sim_dates[0].date()),
        end_date=str(sim_dates[-1].date()),
        trading_days=n_days,
        symbols_tested=len(stock_tickers),
        initial_cash=cfg.initial_cash,
        final_equity=round(final_eq, 2),
        total_return_pct=round(tot_ret, 2),
        cagr_pct=round(cagr, 2),
        benchmark_return_pct=round(bench_ret, 2),
        alpha_vs_bist_pct=round(tot_ret - bench_ret, 2),
        ppf_only_return_pct=round(ppf_only, 2),
        ppf_earned_tl=round(ppf_earned, 2),
        trading_return_pct=round(sum(t["pnl"] for t in closed_trades) / cfg.initial_cash * 100.0, 2),
        max_drawdown_pct=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        sortino_ratio=round(sortino, 2),
        total_trades=n_trades,
        win_rate_pct=round(win_rate, 1),
        profit_factor=round(pf, 2),
        expectancy_pct=round(expectancy, 2),
        avg_win_pct=round(avg_win, 2),
        avg_loss_pct=round(avg_loss, 2),
        avg_hold_days=round(avg_hold, 1),
        exit_reasons=reasons,
        channel_stats=ch_stats,
        top_winners=top_w,
        equity_curve=eq_df,
    )


def format_backtest_report(r: BacktestResult) -> str:
    lines = [
        "📈 Deterministik BIST Backtest Karnesi (0 LLM Çağrısı)",
        f"Dönem: {r.start_date} → {r.end_date} ({r.trading_days} işlem günü, {r.symbols_tested} BIST hissesi)",
        "",
        f"💰 Başlangıç Sermayesi: {r.initial_cash:,.0f} TL",
        f"🏁 Dönem Sonu Özsermaye: {r.final_equity:,.0f} TL ({r.total_return_pct:+.2f}% | CAGR {r.cagr_pct:+.2f}%)",
        f"📊 BIST100 (XU100) Aynı Dönem: {r.benchmark_return_pct:+.2f}%",
        f"🚀 Endeks Üstü Alfa: {r.alpha_vs_bist_pct:+.2f} puan",
        f"🏦 Boştaki Nakit PPF Katkısı: +{r.ppf_earned_tl:,.0f} TL (Sadece PPF tutulsaydı: {r.ppf_only_return_pct:+.2f}%)",
        f"🧮 Hisse işlemlerinin katkısı (faiz hariç): {r.trading_return_pct:+.2f}% "
        f"→ BIST100'e göre {r.trading_return_pct - r.benchmark_return_pct:+.2f} puan",
    ]
    verdict = []
    if r.ppf_only_return_pct > 0 and r.total_return_pct < r.ppf_only_return_pct:
        verdict.append(f"⚠️ Strateji, parayı yalnızca PPF'de tutmaktan {r.ppf_only_return_pct - r.total_return_pct:.1f} "
                       "puan geride: hisse seçimi faizi yenemiyor.")
    if r.trading_return_pct < 0:
        verdict.append("⚠️ Hisse işlemleri tek başına zararda; toplam getirinin kaynağı nakit faizi.")
    if r.profit_factor < 1:
        verdict.append(f"⚠️ Profit factor {r.profit_factor:.2f} < 1: işlemlerin toplam kârı toplam zararından az.")
    verdict.append("ℹ️ Ayarlar aynı veri üzerinde seçildi (örneklem içi); gelecekte aynı sonuç garanti değil. "
                   "Bu test yalnızca Python taramasını ölçer, yapay zekâ kararlarını değil.")
    lines += verdict + [
        "",
        "🛡️ Risk & Kalite Metrikleri",
        f"• Maksimum Düşüş (Max Drawdown): {r.max_drawdown_pct:.2f}%",
        f"• Sharpe Oranı (PPF üstü): {r.sharpe_ratio:.2f} | Sortino: {r.sortino_ratio:.2f}",
        f"• Toplam İşlem: {r.total_trades} | Kazanma Oranı: %{r.win_rate_pct:.1f}",
        f"• Profit Factor: {r.profit_factor:.2f} | İşlem Başı Beklenti: {r.expectancy_pct:+.2f}%",
        f"• Ort. Kazanç: {r.avg_win_pct:+.2f}% | Ort. Kayıp: {r.avg_loss_pct:+.2f}% | Ort. Taşıma: {r.avg_hold_days:.1f} gün",
    ]
    if r.channel_stats:
        lines += ["", "🧭 Kanal Performansı"]
        for ch, st in r.channel_stats.items():
            lines.append(f"• {ch}: {st['trades']} işlem, kazanma %{st['win_rate_pct']}, ort. {st['avg_pnl_pct']:+.2f}%")
    if r.exit_reasons:
        reasons_str = ", ".join(f"{k}: {v}" for k, v in sorted(r.exit_reasons.items(), key=lambda x: -x[1]))
        lines += ["", f"🚪 Çıkış Dağılımı: {reasons_str}"]
    if r.top_winners:
        lines += ["", "🏆 En İyi 5 İşlem"]
        for t in r.top_winners:
            lines.append(f"• {t['ticker']} ({t['entry_date']}→{t['exit_date']}, {t['reason']}): {t['pnl_pct']:+.2f}%")
    return "\n".join(lines)
