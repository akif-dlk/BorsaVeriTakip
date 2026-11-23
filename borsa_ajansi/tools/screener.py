"""BIST evren taraması: LLM'e girmeden önce deterministik puanlama ile aday seçimi (yalnızca uzun/AL yönü).

Puan bileşenleri (toplam ~100):
  trend 30 (kapanış > MA50 > MA200), momentum 25 (MACD hist > 0, RSI 50-68 ideal bölge),
  göreli güç 25 (BIST100'e göre 20g), hacim 10 (hacim oranı >= 1.2), aşırılık cezası (RSI > 75, BB üst bant dışı).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import pandas as pd

from config.settings import settings
from tools.market_data import compute_indicators, fetch_price_history

log = logging.getLogger(__name__)

# 2026 itibarıyla likit BIST30 hisseleri (UNIVERSE ortam değişkeniyle değiştirilebilir)
BIST30 = [
    "AKBNK", "ALARK", "ARCLK", "ASELS", "ASTOR", "BIMAS", "CCOLA", "EKGYO", "ENKAI", "EREGL",
    "FROTO", "GARAN", "GUBRF", "ISCTR", "KCHOL", "KRDMD", "MGROS", "OYAKC", "PETKM", "PGSUS",
    "SAHOL", "SASA", "SISE", "TAVHL", "TCELL", "THYAO", "TOASO", "TTKOM", "TUPRS", "YKBNK",
]


def universe() -> list[str]:
    names = settings.universe or BIST30
    return [n if "." in n else f"{n}.IS" for n in names]


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def score_indicators(ind: dict[str, Any], bist_change_20d: float | None) -> tuple[float, list[str]]:
    if ind.get("status") != "ok":
        return -1.0, ["veri_yok"]
    s, why = 0.0, []
    c, ma50, ma200 = ind.get("close"), ind.get("ma50"), ind.get("ma200")
    if _num(ma50) and c > ma50:
        s += 15; why.append("MA50 üstü")
    if _num(ma200) and c > ma200:
        s += 10; why.append("MA200 üstü")
    if _num(ma50) and _num(ma200) and ma50 > ma200:
        s += 5; why.append("altın kesişim yapısı")
    if _num(ind.get("macd_hist")) and ind["macd_hist"] > 0:
        s += 10; why.append("MACD pozitif")
    rsi = ind.get("rsi14")
    if _num(rsi):
        if 50 <= rsi <= 68:
            s += 15; why.append(f"RSI sağlıklı ({rsi})")
        elif 45 <= rsi < 50 or 68 < rsi <= 75:
            s += 7
        elif rsi > 75:
            s -= 10; why.append(f"RSI aşırı alım ({rsi})")
    ch20 = ind.get("change_20d_pct")
    if _num(ch20) and bist_change_20d is not None:
        rel = ch20 - bist_change_20d
        s += max(-10.0, min(25.0, rel * 2.5))
        why.append(f"BIST100'e göre {rel:+.1f} puan")
    if _num(ind.get("volume_ratio")) and ind["volume_ratio"] >= 1.2:
        s += 10; why.append(f"hacim {ind['volume_ratio']}x")
    if _num(ind.get("bb_upper")) and c > ind["bb_upper"]:
        s -= 5; why.append("Bollinger üst bant dışı")
    return round(s, 2), why


def screen(tickers: list[str] | None = None, top_n: int | None = None,
           fetcher: Callable[[str, str], pd.DataFrame] = fetch_price_history,
           exclude: set[str] | None = None) -> list[dict[str, Any]]:
    tickers = tickers or universe()
    exclude = exclude or set()
    top_n = top_n or settings.sim_candidates
    bist = compute_indicators(fetcher("XU100.IS", "1y"))
    b20 = bist.get("change_20d_pct") if _num(bist.get("change_20d_pct")) else None

    def _one(t: str) -> dict[str, Any]:
        ind = compute_indicators(fetcher(t, "1y"))
        sc, why = score_indicators(ind, b20)
        return {"ticker": t, "score": sc, "reasons": why, "close": ind.get("close"), "rsi14": ind.get("rsi14")}

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(_one, [t for t in tickers if t not in exclude]))
    rows = [r for r in rows if r["score"] >= 0]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top_n]
