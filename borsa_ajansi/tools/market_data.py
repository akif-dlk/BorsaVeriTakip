"""Fiyat, hacim ve teknik indikatörler. TÜM matematik burada (pandas/ta) yapılır; LLM'e hesap yaptırılmaz."""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
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


def _prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def expected_last_bar(now: datetime | None = None) -> date:
    """BIST için şu an elde olması gereken en son günlük mumun tarihi.

    Seans açıldıysa (hafta içi 10:00 sonrası) bugünün mumu, değilse bir önceki iş gününün mumu.
    Resmî tatiller bilinmez: tatilde canlı veri de bugünü içermez, sadece fazladan bir ağ isteği olur.
    """
    now = (now or datetime.now(_TR)).astimezone(_TR)
    if now.weekday() < 5 and now.time() >= _SESSION_OPEN:
        return now.date()
    return _prev_business_day(now.date())


def _warehouse_is_fresh(ticker: str, df: pd.DataFrame, now: datetime | None = None) -> bool:
    """Yerel ambar yalnızca BIST hissesi/endeksi için ve son mum beklenen tarihteyse kullanılır.

    Küresel semboller (VIX, S&P, kur, emtia) farklı saat dilimlerinde işlem gördüğü ve 18:20 senkronunda
    yarım mum yazılabildiği için her zaman canlı çekilir.
    """
    if df.empty or not ticker.upper().endswith(".IS"):
        return False
    last = df.index[-1]
    last_d = last.date() if hasattr(last, "date") else last
    return last_d >= expected_last_bar(now)


def _without_open_bar(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Kapanmamış seansın yarım mumunu ambara yazmamak için çıkarır."""
    if df.empty:
        return df
    last = df.index[-1]
    last_d = last.date() if hasattr(last, "date") else last
    return df.iloc[:-1] if session_progress(last_d, now) is not None else df


def fetch_price_history(ticker: str, period: str = "1y", now: datetime | None = None) -> pd.DataFrame:
    """OHLCV: yerel ambar GÜNCELSE oradan (ağ yok), değilse yfinance'ten canlı; canlı başarısızsa en fazla
    5 günlük ambar verisi uyarıyla kullanılır. Hata/boş veri durumunda boş DataFrame döner (uydurma yok)."""
    from config.settings import settings

    wh, cached = None, pd.DataFrame()
    if settings.use_local_warehouse:
        try:
            from tools.data_warehouse import get_warehouse

            wh = get_warehouse()
            cached = wh.get_ohlcv(ticker, period=period)
            if _warehouse_is_fresh(ticker, cached, now):
                return cached
        except Exception as exc:
            log.debug("Yerel ambar okunamadı %s: %s", ticker, exc)

    import yfinance as yf

    df = pd.DataFrame()
    try:
        raw = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        if raw is not None and not raw.empty:
            df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    except Exception as exc:  # ağ / sembol hatası
        log.warning("Fiyat verisi alınamadı %s: %s", ticker, exc)

    if df.empty:
        if not cached.empty:
            log.warning("%s canlı veri yok; yerel ambardaki son veri kullanılıyor (son mum %s)",
                        ticker, cached.index[-1].date())
            return cached
        return pd.DataFrame()

    if wh is not None:
        try:
            wh.upsert_ohlcv(ticker, _without_open_bar(df, now))
        except Exception as exc:
            log.debug("Yerel ambara yazılamadı %s: %s", ticker, exc)
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


_TR = timezone(timedelta(hours=3))
_SESSION_OPEN, _SESSION_CLOSE = time(10, 0), time(18, 10)  # BIST pay piyasası (kapanış seansı dahil)
_SESSION_MIN = (18 * 60) - (10 * 60)  # sürekli işlem: 10:00-18:00 = 480 dk
_MIN_FRACTION_FOR_ESTIMATE = 0.25  # seansın ilk ~2 saatinde orantılama güvenilmez (açılış hacmi yüksek)


def session_progress(bar_date: date, now: datetime | None = None) -> float | None:
    """Son mum bugünün henüz kapanmamış seansına aitse geçen seans oranını (0-1) döner, değilse None.

    Not: tatil günleri dikkate alınmaz; tatilde yfinance bugünün mumunu vermediği için sorun olmaz.
    """
    now = (now or datetime.now(_TR)).astimezone(_TR)
    if bar_date != now.date() or now.weekday() >= 5:
        return None
    t = now.time()
    if t >= _SESSION_CLOSE:
        return None  # seans kapandı, mum tamamlandı
    if t < _SESSION_OPEN:
        return 0.0
    elapsed = (now.hour * 60 + now.minute) - 10 * 60
    return max(0.0, min(1.0, elapsed / _SESSION_MIN))


def compute_indicators(df: pd.DataFrame, now: datetime | None = None) -> dict[str, Any]:
    """Saf fonksiyon: OHLCV DataFrame -> indikatör sözlüğü. Yetersiz veri alanları 'veri_yok' olur.

    Seans içinde çağrılırsa son mum yarımdır: fiyat göstergeleri anlık fiyatla hesaplanır, ama hacim
    tam günlük ortalamayla doğrudan kıyaslanmaz (bkz. volume_basis).
    """
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
    last_idx = df.index[-1]
    bar_date = last_idx.date() if hasattr(last_idx, "date") else None
    progress = session_progress(bar_date, now) if bar_date else None
    intraday = progress is not None

    avg_vol20 = vol.tail(21).iloc[:-1].mean() if n >= 21 else None
    vol_ratio, vol_basis = None, "tam_gun"
    if avg_vol20:
        if not intraday:
            vol_ratio = float(vol.iloc[-1]) / float(avg_vol20)
        elif progress >= _MIN_FRACTION_FOR_ESTIMATE:
            # yarım günün hacmi, ortalamanın geçen seans oranı kadarıyla kıyaslanır
            vol_ratio = float(vol.iloc[-1]) / (float(avg_vol20) * progress)
            vol_basis = "seans_ici_orantili_tahmin"
        elif n >= 22:
            # seans çok yeni: son TAMAMLANMIŞ günün hacmini, ondan önceki 20 günle kıyasla
            prev_avg = vol.iloc[-22:-2].mean()
            vol_ratio = float(vol.iloc[-2]) / float(prev_avg) if prev_avg else None
            vol_basis = "onceki_tam_gun"
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
        # tam_gun | seans_ici_orantili_tahmin | onceki_tam_gun
        "volume_basis": vol_basis,
        # True: son mum seans içi (kapanmamış); close = anlık fiyat, günlük değişim/RSI geçicidir
        "intraday_bar": intraday,
        "session_elapsed_pct": _clean(progress * 100, 0) if intraday else VERI_YOK,
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
        "volume_basis": ind.get("volume_basis"),
    }


def get_market_snapshot(ticker: str, period: str = "1y") -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    df = fetch_price_history(ticker, period)
    ind = compute_indicators(df)
    return {"ticker": ticker, "indicators": ind, "sentiment_metrics": compute_sentiment_metrics(ind)}
