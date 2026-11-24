"""Teknik göstergelerin Python başvuru (referans) uygulaması.

`marts.mart_indicators` (dbt, SQL) ile **aynı tanımları** kullanır; iki amaçla tutulur:
- SQL modelinin doğruluğunu test etmek (`tests/test_mart_indicators_pg.py` aynı veriyle ikisini karşılaştırır),
- veritabanı dışında (akış verisi, API, UI) aynı göstergeleri tutarlı hesaplamak.

Tanımlar (her enstrüman kendi işlem günleri üzerinde; tatiller ve eksik günler atlanır, ileri doldurma yok):
- **SMA(n):** son n kapanışın aritmetik ortalaması; n gözlem dolmadan NULL.
- **EMA(n):** α = 2/(n+1), ilk kapanışla tohumlanır (pandas `ewm(span=n, adjust=False)`); ilk n-1 gün NULL.
- **RSI(14), Wilder:** kazanç/kayıp ortalamaları α = 1/14 ile üstel düzleştirilir, ilk fiyat farkıyla tohumlanır
  (pandas `ewm(alpha=1/14, adjust=False)`, `ta.momentum.RSIIndicator` ile aynı); 14 fark dolmadan NULL.
  Hiç kayıp yoksa 100, hiç hareket yoksa 50.
- **Volatilite(n):** son n günlük log getirinin örneklem standart sapması × √252 (yıllıklandırılmış);
  n getiri dolmadan NULL.
Hepsi düzeltilmiş kapanış (`adj_close`) üzerinden hesaplanır.
"""
from __future__ import annotations

import math

import pandas as pd

SMA_WINDOWS = (20, 50, 200)
EMA_SPANS = (12, 26)
RSI_WINDOW = 14
VOLATILITY_WINDOW = 20
TRADING_DAYS_PER_YEAR = 252
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# mart_indicators'ın gösterge sütunları (sıra SQL modelindekiyle aynı)
INDICATOR_COLUMNS = (
    "log_return",
    *(f"sma_{n}" for n in SMA_WINDOWS),
    *(f"ema_{n}" for n in EMA_SPANS),
    f"rsi_{RSI_WINDOW}",
    f"volatility_{VOLATILITY_WINDOW}d",
)


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window, min_periods=window).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def log_returns(close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    valid = (close > 0) & (prev > 0)
    return pd.Series(
        [math.log(c / p) if ok else math.nan for c, p, ok in zip(close, prev, valid)],
        index=close.index, dtype="float64",
    )


def rsi(close: pd.Series, window: int = RSI_WINDOW) -> pd.Series:
    delta = close.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = (-delta).clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    out = 100 - 100 / (1 + avg_gain / avg_loss)
    out = out.mask(avg_loss == 0, 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out.where(avg_gain.notna())


def volatility(close: pd.Series, window: int = VOLATILITY_WINDOW) -> pd.Series:
    return log_returns(close).rolling(window, min_periods=window).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)


def rsi_zone(value: float | None) -> str | None:
    """RSI'ı bölgeye çevirir: overbought (≥70), oversold (≤30), neutral; değer yoksa None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if value >= RSI_OVERBOUGHT:
        return "overbought"
    if value <= RSI_OVERSOLD:
        return "oversold"
    return "neutral"


def compute_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """`instrument_code, price_date, adj_close` sütunlu fiyatlardan mart_indicators'ın gösterge sütunlarını üretir.

    Her enstrüman tarih sırasına göre ayrı işlenir. Dönen çerçeve girdinin sütunlarına ek olarak
    `obs_index` (1'den başlayan gözlem sırası), INDICATOR_COLUMNS ve `rsi_zone` taşır.
    """
    required = {"instrument_code", "price_date", "adj_close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"eksik sütun(lar): {sorted(missing)}")

    frames = []
    for _, group in prices.sort_values(["instrument_code", "price_date"]).groupby("instrument_code", sort=True):
        g = group.reset_index(drop=True).copy()
        close = g["adj_close"].astype("float64")
        g["obs_index"] = range(1, len(g) + 1)
        g["log_return"] = log_returns(close)
        for n in SMA_WINDOWS:
            g[f"sma_{n}"] = sma(close, n)
        for n in EMA_SPANS:
            g[f"ema_{n}"] = ema(close, n)
        g[f"rsi_{RSI_WINDOW}"] = rsi(close)
        g[f"volatility_{VOLATILITY_WINDOW}d"] = volatility(close)
        g["rsi_zone"] = [rsi_zone(v) for v in g[f"rsi_{RSI_WINDOW}"]]
        frames.append(g)
    if not frames:
        return prices.assign(obs_index=pd.Series(dtype="int64"),
                             **{c: pd.Series(dtype="float64") for c in INDICATOR_COLUMNS},
                             rsi_zone=pd.Series(dtype="object"))
    return pd.concat(frames, ignore_index=True)
