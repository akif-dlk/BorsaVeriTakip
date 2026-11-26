"""BIST evren taraması: LLM'e girmeden önce deterministik puanlama ile aday seçimi (yalnızca uzun/AL yönü).

Puan bileşenleri (toplam ~100):
  trend 30 (kapanış > MA50 > MA200), momentum 25 (MACD hist > 0, RSI 50-68 ideal bölge),
  göreli güç 25 (BIST100'e göre 20g), hacim 10 (hacim oranı >= 1.2), aşırılık cezası (RSI > 75, BB üst bant dışı).
Sığ hisseler (20 günlük ort. işlem hacmi < SCREEN_MIN_TURNOVER_MTL milyon TL) elenir.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import pandas as pd

from config.settings import settings
from tools.market_data import compute_indicators, fetch_price_history

log = logging.getLogger(__name__)

# 2026 itibarıyla likit BIST30 hisseleri
BIST30 = [
    "AKBNK", "ALARK", "ARCLK", "ASELS", "ASTOR", "BIMAS", "CCOLA", "EKGYO", "ENKAI", "EREGL",
    "FROTO", "GARAN", "GUBRF", "ISCTR", "KCHOL", "KRDMD", "MGROS", "OYAKC", "PETKM", "PGSUS",
    "SAHOL", "SASA", "SISE", "TAVHL", "TCELL", "THYAO", "TOASO", "TTKOM", "TUPRS", "YKBNK",
]

# BIST100 (Kasım 2026 itibarıyla yaklaşık bileşim; endeks çeyreklik değişir, eksik/fazla birkaç hisse olabilir).
# Verisi olmayan sembol taramada kendiliğinden elenir.
BIST100 = sorted(set(BIST30 + [
    "AEFES", "AGHOL", "AGROT", "AHGAZ", "AKSA", "AKSEN", "ALFAS", "ALTNY", "ANSGR", "ARDYZ",
    "AVPGY", "BALSU", "BERA", "BINHO", "BRSAN", "BRYAT", "BSOKE", "BTCIM", "CANTE", "CIMSA",
    "CLEBI", "CWENE", "DOAS", "DOHOL", "DSTKF", "ECILC", "EGEEN", "ENERY", "ENJSA", "ENTRA",
    "EUPWR", "FENER", "GESAN", "GLRMK", "GOLTS", "GRSEL", "GRTHO", "GSRAY", "HALKB", "HEKTS",
    "ISMEN", "IZENR", "KCAER", "KLRHO", "KONTR", "KONYA", "KTLEV", "LMKDC", "MAGEN", "MAVI",
    "MIATK", "MPARK", "OBAMS", "ODAS", "OTKAR", "PASEU", "PATEK", "QUAGR", "RALYH", "REEDR",
    "SELEC", "SKBNK", "SMRTG", "SOKM", "TABGD", "TKFEN", "TRALT", "TRMET", "TSKB", "TTRAK",
    "TUKAS", "TURSG", "ULKER", "VAKBN", "VESTL", "YEOTK", "ZOREN",
]))


def universe() -> list[str]:
    names = settings.universe or BIST100
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


def score_reversal_catalyst(
    ind: dict[str, Any],
    kap_items: list[dict[str, Any]] | None = None,
) -> tuple[float, list[str]]:
    """2. Kanal: Aşırı satım destek dönüşü (mean-reversion) + yakın dönem KAP katalizörü puanlaması."""
    if ind.get("status") != "ok":
        return -1.0, ["veri_yok"]
    s, why = 0.0, []
    c = ind.get("close")
    rsi = ind.get("rsi14")
    stoch = ind.get("stoch_k")
    ch1 = ind.get("change_1d_pct")
    vol_r = ind.get("volume_ratio")
    lv = ind.get("levels") or {}
    s60 = lv.get("support_60d")
    bb_low = ind.get("bb_lower")

    oversold = (_num(rsi) and rsi <= 36) or (_num(stoch) and stoch <= 22)
    near_bb_low = _num(bb_low) and _num(c) and c <= bb_low * 1.03
    if oversold:
        s += 25
        why.append(f"aşırı satım dönüş bölgesi (RSI={rsi})")
    elif _num(rsi) and rsi <= 42 and near_bb_low:
        s += 15
        why.append("Bollinger alt bant desteği")

    if _num(ch1) and ch1 > 0.3:
        s += 15
        why.append(f"tepki mumu ({ch1:+.1f}%)")
    if _num(s60) and _num(c) and s60 > 0 and 0 <= (c - s60) / s60 <= 0.06:
        s += 15
        why.append("60g ana destek üzerinde")
    if _num(vol_r) and vol_r >= 1.1:
        s += 10
        why.append(f"dönüş hacmi {vol_r}x")

    if kap_items:
        from agents.document_reader import _HIGH_MAT_KAP, _NEG_KAP, _POS_KAP

        pos_hits, high_hits, neg_hits = 0, 0, 0
        for item in kap_items[:3]:
            blob = f"{item.get('title', '')} {item.get('summary', '')} {item.get('body_text', '')}"
            pos_hits += len(_POS_KAP.findall(blob))
            neg_hits += len(_NEG_KAP.findall(blob))
            if _HIGH_MAT_KAP.search(blob):
                high_hits += 1
        if pos_hits > neg_hits:
            boost = 25 if high_hits else 15
            s += boost
            why.append("olumlu KAP katalizörü")
        elif neg_hits > pos_hits:
            s -= 20

    # Dönüş/katalizör adayı sayılabilmesi için anlamlı sinyal eşiği
    return (round(s, 2), why) if s >= 35 else (-1.0, why)


def screen(tickers: list[str] | None = None, top_n: int | None = None,
           fetcher: Callable[[str, str], pd.DataFrame] = fetch_price_history,
           exclude: set[str] | None = None,
           include_reversals: bool | None = None,
           prefilter_rr: bool | None = None,
           kap_catalysts: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    from agents.risk_manager import compute_trade_plan

    tickers = tickers or universe()
    exclude = exclude or set()
    top_n = top_n or settings.sim_candidates
    inc_rev = settings.screen_include_reversals if include_reversals is None else include_reversals
    chk_rr = settings.screen_prefilter_rr if prefilter_rr is None else prefilter_rr

    if inc_rev and kap_catalysts is None and settings.use_local_warehouse:
        try:
            from tools.data_warehouse import get_warehouse

            kap_catalysts = get_warehouse().recent_kap_catalysts(days=7)
        except Exception:
            kap_catalysts = {}
    kap_catalysts = kap_catalysts or {}

    bist = compute_indicators(fetcher("XU100.IS", "1y"))
    b20 = bist.get("change_20d_pct") if _num(bist.get("change_20d_pct")) else None

    min_to = settings.screen_min_turnover_mtl * 1e6

    def _one(t: str) -> dict[str, Any]:
        ind = compute_indicators(fetcher(t, "1y"))
        c, av = ind.get("close"), ind.get("avg_volume_20d")
        if min_to > 0 and _num(c) and _num(av) and c * av < min_to:
            return {"ticker": t, "score": -1.0, "rev_score": -1.0, "reasons": ["sığ (düşük işlem hacmi)"],
                    "rev_reasons": [], "close": c, "rsi14": None, "rr": None, "channel": "elenen"}
        plan = compute_trade_plan("AL", ind)
        rr = plan.risk_reward if plan else None
        if chk_rr and (rr is None or rr < settings.min_risk_reward):
            return {"ticker": t, "score": -1.0, "rev_score": -1.0, "reasons": [f"R/Ö yetersiz ({rr})"],
                    "rev_reasons": [], "close": c, "rsi14": ind.get("rsi14"), "rr": rr, "channel": "elenen"}
        sc, why = score_indicators(ind, b20)
        rev_sc, rev_why = score_reversal_catalyst(ind, kap_catalysts.get(t)) if inc_rev else (-1.0, [])
        return {
            "ticker": t,
            "score": sc,
            "rev_score": rev_sc,
            "reasons": why,
            "rev_reasons": rev_why,
            "close": ind.get("close"),
            "rsi14": ind.get("rsi14"),
            "rr": rr,
            "channel": "trend_momentum",
        }

    with ThreadPoolExecutor(max_workers=10) as pool:
        all_rows = list(pool.map(_one, [t for t in tickers if t not in exclude]))

    mom_rows = [dict(r) for r in all_rows if r["score"] >= 0]
    mom_rows.sort(key=lambda r: r["score"], reverse=True)

    if not inc_rev or top_n < 2:
        return mom_rows[:top_n]

    rev_slots = max(1, top_n // 3)
    mom_slots = max(1, top_n - rev_slots)
    chosen = mom_rows[:mom_slots]
    picked_set = {r["ticker"] for r in chosen}

    rev_rows = [dict(r) for r in all_rows if r["rev_score"] >= 35 and r["ticker"] not in picked_set]
    rev_rows.sort(key=lambda r: r["rev_score"], reverse=True)
    for r in rev_rows[:rev_slots]:
        r["score"] = r["rev_score"]
        r["reasons"] = r["rev_reasons"]
        r["channel"] = "donus_katalizor"
        chosen.append(r)
        picked_set.add(r["ticker"])

    # Dönüş kanalı boşsa kalan kontenjanı momentum adaylarıyla tamamla
    for r in mom_rows[mom_slots:]:
        if len(chosen) >= top_n:
            break
        if r["ticker"] not in picked_set:
            chosen.append(r)
            picked_set.add(r["ticker"])
    return chosen[:top_n]
