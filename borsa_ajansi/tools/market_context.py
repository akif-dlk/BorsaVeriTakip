"""Küresel ve yerel piyasa bağlamı: endeksler, kur, emtia, faiz, volatilite + hissenin göreli gücü.

Tüm değişimler Python'da hesaplanır. Sonuç 15 dk önbelleklenir (tarama sırasında tekrar indirilmesin).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pandas as pd

from config.settings import VERI_YOK
from tools.market_data import _clean, compute_indicators, fetch_price_history

INSTRUMENTS: dict[str, str] = {
    "XU100.IS": "bist100",
    "XU030.IS": "bist30",
    "XBANK.IS": "bist_banka",
    "USDTRY=X": "usdtry",
    "EURTRY=X": "eurtry",
    "GC=F": "altin_ons",
    "BZ=F": "brent_petrol",
    "^GSPC": "sp500",
    "^IXIC": "nasdaq",
    "^GDAXI": "dax",
    "^VIX": "vix",
    "DX-Y.NYB": "dolar_endeksi",
    "^TNX": "abd_10y_faiz",
}

_CACHE: dict[str, Any] = {"t": 0.0, "data": None}
_LOCK = threading.Lock()
TTL_SECONDS = 900


def _summary(ind: dict[str, Any]) -> dict[str, Any]:
    if ind.get("status") != "ok":
        return {"status": VERI_YOK}
    return {
        "status": "ok",
        "date": ind["date"],
        "close": ind["close"],
        "change_1d_pct": ind["change_1d_pct"],
        "change_5d_pct": ind["change_5d_pct"],
        "change_20d_pct": ind["change_20d_pct"],
        "rsi14": ind["rsi14"],
        "above_ma50": ind["above_ma50"],
    }


def build_global_context(fetcher: Callable[[str, str], pd.DataFrame] = fetch_price_history) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sym, name in INSTRUMENTS.items():
        out[name] = _summary(compute_indicators(fetcher(sym, "6mo")))
    ok = [v for v in out.values() if v.get("status") == "ok"]
    out["status"] = "ok" if ok else VERI_YOK
    # Basit, deterministik risk iştahı etiketi
    vix = out.get("vix", {}).get("close")
    spx = out.get("sp500", {}).get("change_5d_pct")
    if isinstance(vix, (int, float)) and isinstance(spx, (int, float)):
        out["global_risk_regime"] = "risk_off" if vix >= 25 or spx <= -3 else "risk_on" if vix < 18 and spx > 0 else "notr"
    else:
        out["global_risk_regime"] = VERI_YOK
    out.update(compute_bist_regime(out))
    return out


def compute_bist_regime(ctx: dict[str, Any]) -> dict[str, Any]:
    """BIST100, XBANK ve küresel risk iştahına göre yerel piyasa rejimi ve dinamik risk çarpanı (Python)."""
    bist = ctx.get("bist100") or {}
    bank = ctx.get("bist_banka") or {}
    glob = ctx.get("global_risk_regime", VERI_YOK)
    if bist.get("status") != "ok":
        return {"bist_regime": VERI_YOK, "recommended_risk_mult": 1.0}

    above_ma50 = bist.get("above_ma50")
    ch5 = bist.get("change_5d_pct")
    bank_ch5 = bank.get("change_5d_pct") if bank.get("status") == "ok" else 0.0

    bearish_bist = above_ma50 is False
    bank_selloff = isinstance(bank_ch5, (int, float)) and bank_ch5 <= -4.0
    if glob == "risk_off" or bearish_bist or bank_selloff:
        return {"bist_regime": "defansif", "recommended_risk_mult": 0.5}
    if above_ma50 is True and glob != "risk_off" and isinstance(ch5, (int, float)) and ch5 >= 0:
        return {"bist_regime": "boga", "recommended_risk_mult": 1.0}
    return {"bist_regime": "notr", "recommended_risk_mult": 0.75}


def get_global_context(fetcher: Callable[[str, str], pd.DataFrame] = fetch_price_history) -> dict[str, Any]:
    with _LOCK:
        if _CACHE["data"] is not None and time.time() - _CACHE["t"] < TTL_SECONDS:
            return _CACHE["data"]
        data = build_global_context(fetcher)
        _CACHE.update(t=time.time(), data=data)
        return data


def relative_strength(stock_ind: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Hissenin BIST100'e göre 5/20 günlük göreli performansı (puan farkı)."""
    bist = ctx.get("bist100", {})
    out: dict[str, Any] = {}
    for k in ("change_5d_pct", "change_20d_pct"):
        a, b = stock_ind.get(k), bist.get(k)
        key = "rel_strength_" + k.replace("change_", "").replace("_pct", "") + "_pct"
        out[key] = _clean(a - b, 2) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else VERI_YOK
    return out
