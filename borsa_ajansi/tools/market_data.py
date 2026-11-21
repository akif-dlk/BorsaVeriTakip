"""Fiyat, hacim ve teknik indikatörler. TÜM matematik burada (pandas/ta) yapılır; LLM'e hesap yaptırılmaz."""
from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from config.settings import VERI_YOK

log = logging.getLogger(__name__)


def _clean(v: Any, nd: int = 4) -> Any:
    """NaN/inf -> 'veri_yok', float'ları yuvarla."""
    if v is None:
        return VERI_YOK
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if math.isnan(f) or math.isinf(f):
        return VERI_YOK
    return round(f, nd)


def normalize_ticker(ticker: str) -> str:
    t = ticker.strip().upper()
    # Nokta yoksa BIST varsay (THYAO -> THYAO.IS); global semboller (AAPL) için .US yazmaya gerek yok
    if "." not in t and t.isalpha() and len(t) in (4, 5) and not t.startswith("^"):
        return f"{t}.IS"
    return t


def fetch_price_history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """yfinance'ten OHLCV çeker. Hata/boş veri durumunda boş DataFrame döner (uydurma yok)."""
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    except Exception as exc:  # ağ / sembol hatası
        log.warning("Fiyat verisi alınamadı %s: %s", ticker, exc)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    return df


def _support_resistance(df: pd.DataFrame) -> dict[str, Any]:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    return {
        "pivot": _clean(pivot),
        "r1": _clean(2 * pivot - prev["low"]),
        "s1": _clean(2 * pivot - prev["high"]),
        "support_20d": _clean(df["low"].tail(20).min()),
        "resistance_20d": _clean(df["high"].tail(20).max()),
        "support_60d": _clean(df["low"].tail(60).min()),
        "resistance_60d": _clean(df["high"].tail(60).max()),
        "last_close": _clean(last["close"]),
    }


def compute_indicators(df: pd.DataFrame) -> dict[str, Any]:
    """Saf fonksiyon: OHLCV DataFrame -> indikatör sözlüğü. Yetersiz veri alanları 'veri_yok' olur."""
    if df is None or df.empty or len(df) < 2:
        return {"status": VERI_YOK}

    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    n = len(df)

    def sma(w: int):
        return SMAIndicator(close, window=w).sma_indicator().iloc[-1] if n >= w else None

    rsi = RSIIndicator(close, window=14).rsi().iloc[-1] if n >= 15 else None
    macd_obj = MACD(close) if n >= 35 else None
    atr = AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1] if n >= 15 else None
    bb = BollingerBands(close, window=20) if n >= 20 else None
    stoch = StochasticOscillator(high, low, close).stoch().iloc[-1] if n >= 17 else None

    last_close = float(close.iloc[-1])
    avg_vol20 = vol.tail(21).iloc[:-1].mean() if n >= 21 else None
    vol_ratio = (float(vol.iloc[-1]) / float(avg_vol20)) if avg_vol20 else None
    ma20, ma50, ma200 = sma(20), sma(50), sma(200)

    def pct_change(k: int):
        return (last_close / float(close.iloc[-1 - k]) - 1) * 100 if n > k else None

    hi52, lo52 = float(high.tail(252).max()), float(low.tail(252).min())

    out = {
        "status": "ok",
        "date": str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1]),
        "close": _clean(last_close),
        "change_1d_pct": _clean(pct_change(1), 2),
        "change_5d_pct": _clean(pct_change(5), 2),
        "change_20d_pct": _clean(pct_change(20), 2),
        "rsi14": _clean(rsi, 2),
        "macd": _clean(macd_obj.macd().iloc[-1]) if macd_obj else VERI_YOK,
        "macd_signal": _clean(macd_obj.macd_signal().iloc[-1]) if macd_obj else VERI_YOK,
        "macd_hist": _clean(macd_obj.macd_diff().iloc[-1]) if macd_obj else VERI_YOK,
        "ma20": _clean(ma20),
        "ma50": _clean(ma50),
        "ma200": _clean(ma200),
        "above_ma50": bool(last_close > ma50) if ma50 is not None else VERI_YOK,
        "above_ma200": bool(last_close > ma200) if ma200 is not None else VERI_YOK,
        "atr14": _clean(atr),
        "bb_upper": _clean(bb.bollinger_hband().iloc[-1]) if bb else VERI_YOK,
        "bb_lower": _clean(bb.bollinger_lband().iloc[-1]) if bb else VERI_YOK,
        "stoch_k": _clean(stoch, 2),
        "volume": _clean(vol.iloc[-1], 0),
        "avg_volume_20d": _clean(avg_vol20, 0),
        "volume_ratio": _clean(vol_ratio, 2),
        "volume_anomaly": bool(vol_ratio >= 2.0) if vol_ratio is not None else VERI_YOK,
        "high_52w": _clean(hi52),
        "low_52w": _clean(lo52),
        "dist_from_52w_high_pct": _clean((last_close / hi52 - 1) * 100, 2),
        "levels": _support_resistance(df),
    }
    return out


def compute_sentiment_metrics(ind: dict[str, Any]) -> dict[str, Any]:
    """Fiyat tabanlı piyasa psikolojisi metrikleri (deterministik)."""
    if ind.get("status") != "ok":
        return {"status": VERI_YOK}
    rsi, stoch = ind.get("rsi14"), ind.get("stoch_k")
    zone = VERI_YOK
    if isinstance(rsi, (int, float)):
        zone = "asiri_alim" if rsi >= 70 else "asiri_satim" if rsi <= 30 else "notr"
    close, ma50 = ind.get("close"), ind.get("ma50")
    dist_ma50 = _clean((close / ma50 - 1) * 100, 2) if isinstance(ma50, (int, float)) and ma50 else VERI_YOK
    bb_pos = VERI_YOK
    up, lo = ind.get("bb_upper"), ind.get("bb_lower")
    if all(isinstance(x, (int, float)) for x in (up, lo, close)) and up != lo:
        bb_pos = _clean((close - lo) / (up - lo), 3)  # 0=alt bant, 1=üst bant
    return {
        "status": "ok",
        "rsi_zone": zone,
        "rsi14": rsi,
        "stoch_k": stoch,
        "dist_from_ma50_pct": dist_ma50,
        "bollinger_position": bb_pos,
        "dist_from_52w_high_pct": ind.get("dist_from_52w_high_pct"),
        "change_5d_pct": ind.get("change_5d_pct"),
        "volume_ratio": ind.get("volume_ratio"),
    }


def get_market_snapshot(ticker: str, period: str = "1y") -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    df = fetch_price_history(ticker, period)
    ind = compute_indicators(df)
    return {"ticker": ticker, "indicators": ind, "sentiment_metrics": compute_sentiment_metrics(ind)}
