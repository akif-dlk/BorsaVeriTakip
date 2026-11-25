"""Getiri, dönem özeti ve korelasyonların Python başvuru (referans) uygulaması.

dbt modelleriyle **aynı tanımları** kullanır (`tests/test_mart_correlations_pg.py` ikisini aynı veriyle karşılaştırır):
- `marts.mart_returns` ↔ `daily_returns()`: enstrüman × işlem günü getirileri,
- `marts.mart_period_returns` ↔ `period_summary()`: haftalık / aylık özet,
- `marts.mart_correlations` ↔ `rolling_correlations()`: enstrüman çiftleri arasında hareketli korelasyon.
`correlation_matrix()` son korelasyonları ısı haritası/API için kare matrise çevirir.

Tanımlar (hepsi `adj_close` üzerinden; her enstrüman kendi işlem günleri üzerinde, ileri doldurma yok):
- **Günlük getiri:** basit `adj_close / önceki işlem günü - 1`, log `ln(adj_close / önceki)` (bvt.indicators ile
  aynı), birikimli `adj_close / ilk adj_close - 1`. Önceki gün tatil/eksikse getiri aradaki tüm günleri kapsar
  (`days_since_prev` takvim günü farkını gösterir). Fiyat ≤ 0 ise getiri NULL.
- **Dönem özeti:** hafta (ISO, pazartesi başlar) ve ay. Dönem getirisi = dönemin son kapanışı / bir önceki
  (verisi olan) dönemin son kapanışı - 1; ilk dönemde NULL. Volatilite = dönem içindeki günlük log getirilerin
  örneklem std'si × √252 (en az 2 getiri). `is_complete`: dönemin takvim sonu bugünden önce.
- **Korelasyon:** her (a, b) çifti için (a < b, bayt sırası) iki enstrümanın da log getirisi olan ortak
  günlerde, son n ortak gözlemin Pearson korelasyonu; n gözlem dolmadan NULL, serilerden biri sabitse NULL.
  Bilinen sınırlama: farklı takvimler (BIST tatili, ABD vadeli altın) bir günlük getirinin farklı uzunlukta
  aralıkları kapsamasına yol açabilir; korelasyon ortak işlem günlerinde ölçülür.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from bvt.indicators import TRADING_DAYS_PER_YEAR, log_returns

PERIOD_TYPES = ("week", "month")
CORRELATION_WINDOWS = (60, 252)

RETURN_COLUMNS = ("obs_index", "prev_price_date", "days_since_prev", "simple_return", "log_return",
                  "cumulative_return")
PERIOD_COLUMNS = ("instrument_code", "period_type", "period_start_date", "period_end_date", "first_trading_date",
                  "last_trading_date", "n_trading_days", "first_close", "last_close", "max_close", "min_close",
                  "prev_period_close", "period_return", "period_log_return", "volatility", "total_volume",
                  "is_complete")
CORRELATION_COLUMNS = ("instrument_a", "instrument_b", "price_date", "pair_obs_index",
                       *(f"corr_{n}d" for n in CORRELATION_WINDOWS))


def _require(df: pd.DataFrame, columns: set[str]) -> None:
    missing = columns - set(df.columns)
    if missing:
        raise ValueError(f"eksik sütun(lar): {sorted(missing)}")


def _as_date(value) -> date:
    return value.date() if isinstance(value, pd.Timestamp) else value


def period_start(day: date, period_type: str) -> date:
    """Günün ait olduğu dönemin ilk takvim günü (hafta: pazartesi, ay: 1'i)."""
    day = _as_date(day)
    if period_type == "week":
        return day - timedelta(days=day.weekday())
    if period_type == "month":
        return day.replace(day=1)
    raise ValueError(f"bilinmeyen dönem tipi: {period_type!r} (beklenen: {PERIOD_TYPES})")


def period_end(start: date, period_type: str) -> date:
    """Dönemin son takvim günü."""
    if period_type == "week":
        return start + timedelta(days=6)
    if period_type == "month":
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return next_month - timedelta(days=1)
    raise ValueError(f"bilinmeyen dönem tipi: {period_type!r} (beklenen: {PERIOD_TYPES})")


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """`instrument_code, price_date, adj_close` (+ isteğe bağlı `volume`) fiyatlarından günlük getiriler.

    Girdi sütunlarına RETURN_COLUMNS eklenir; satırlar enstrüman ve tarihe göre sıralı döner.
    """
    _require(prices, {"instrument_code", "price_date", "adj_close"})
    frames = []
    for _, group in prices.sort_values(["instrument_code", "price_date"]).groupby("instrument_code", sort=True):
        g = group.reset_index(drop=True).copy()
        close = g["adj_close"].astype("float64")
        prev = close.shift(1)
        g["obs_index"] = range(1, len(g) + 1)
        g["prev_price_date"] = [None, *g["price_date"].iloc[:-1]] if len(g) else []
        g["days_since_prev"] = pd.array(
            [None if p is None else (_as_date(d) - _as_date(p)).days
             for d, p in zip(g["price_date"], g["prev_price_date"])], dtype="Int64")
        g["simple_return"] = (close / prev - 1).where((close > 0) & (prev > 0))
        g["log_return"] = log_returns(close)
        first = close.iloc[0]
        g["cumulative_return"] = close / first - 1 if first > 0 else math.nan
        frames.append(g)
    if not frames:
        return prices.assign(**{c: pd.Series(dtype="object") for c in RETURN_COLUMNS})
    return pd.concat(frames, ignore_index=True)


def period_summary(returns: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    """`daily_returns()` çıktısından haftalık ve aylık özet (PERIOD_COLUMNS).

    `today`: tamamlanmış dönem ayrımı için referans gün (varsayılan: bugün).
    """
    _require(returns, {"instrument_code", "price_date", "adj_close", "log_return"})
    today = today or date.today()
    if returns.empty:
        return pd.DataFrame(columns=list(PERIOD_COLUMNS))
    base = returns.assign(
        adj_close=returns["adj_close"].astype("float64"),
        log_return=returns["log_return"].astype("float64"),
        volume=(returns["volume"] if "volume" in returns else pd.Series(math.nan, index=returns.index))
        .astype("float64"),
    )
    frames = []
    for ptype in PERIOD_TYPES:
        df = base.assign(period_start_date=[period_start(d, ptype) for d in base["price_date"]])
        df = df.sort_values(["instrument_code", "price_date"])
        agg = df.groupby(["instrument_code", "period_start_date"], sort=True).agg(
            first_trading_date=("price_date", "min"),
            last_trading_date=("price_date", "max"),
            n_trading_days=("price_date", "size"),
            first_close=("adj_close", "first"),
            last_close=("adj_close", "last"),
            max_close=("adj_close", "max"),
            min_close=("adj_close", "min"),
            return_std=("log_return", "std"),
            n_returns=("log_return", "count"),
            total_volume=("volume", lambda v: v.sum(min_count=1)),
        ).reset_index()
        agg["period_type"] = ptype
        agg["period_end_date"] = [period_end(s, ptype) for s in agg["period_start_date"]]
        agg["prev_period_close"] = agg.groupby("instrument_code")["last_close"].shift(1)
        valid = (agg["last_close"] > 0) & (agg["prev_period_close"] > 0)
        ratio = agg["last_close"] / agg["prev_period_close"]
        agg["period_return"] = (ratio - 1).where(valid)
        agg["period_log_return"] = [math.log(r) if ok else math.nan for r, ok in zip(ratio, valid)]
        agg["volatility"] = (agg["return_std"] * math.sqrt(TRADING_DAYS_PER_YEAR)).where(agg["n_returns"] >= 2)
        agg["is_complete"] = [e < today for e in agg["period_end_date"]]
        frames.append(agg[list(PERIOD_COLUMNS)])
    return pd.concat(frames, ignore_index=True)


def rolling_correlations(returns: pd.DataFrame, windows: tuple[int, ...] = CORRELATION_WINDOWS) -> pd.DataFrame:
    """`daily_returns()` çıktısından çift bazlı hareketli korelasyonlar (CORRELATION_COLUMNS).

    Her çift (a < b) bir kez yer alır; satırlar ortak (iki tarafta da log getirisi olan) günlerdir.
    """
    _require(returns, {"instrument_code", "price_date", "log_return"})
    r = returns.loc[returns["log_return"].notna(), ["instrument_code", "price_date", "log_return"]]
    r = r.assign(log_return=r["log_return"].astype("float64"))
    by_code = {code: g.drop(columns="instrument_code") for code, g in r.groupby("instrument_code")}
    codes = sorted(by_code)  # Python str sırası = PostgreSQL `collate "C"` (bayt sırası, ASCII kodlar)
    frames = []
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            m = by_code[a].merge(by_code[b], on="price_date", suffixes=("_a", "_b"))
            if m.empty:
                continue
            m = m.sort_values("price_date").reset_index(drop=True)
            out = pd.DataFrame({"instrument_a": a, "instrument_b": b, "price_date": m["price_date"],
                                "pair_obs_index": range(1, len(m) + 1)})
            for n in windows:
                corr = m["log_return_a"].rolling(n, min_periods=n).corr(m["log_return_b"])
                flat = (m["log_return_a"].rolling(n).std() == 0) | (m["log_return_b"].rolling(n).std() == 0)
                out[f"corr_{n}d"] = corr.mask(flat).clip(-1.0, 1.0)
            frames.append(out)
    if not frames:
        return pd.DataFrame({c: pd.Series(dtype="float64" if c.startswith("corr_") else "object")
                             for c in ("instrument_a", "instrument_b", "price_date", "pair_obs_index",
                                       *(f"corr_{n}d" for n in windows))})
    return pd.concat(frames, ignore_index=True)


def correlation_matrix(correlations: pd.DataFrame, window: int = CORRELATION_WINDOWS[0],
                       as_of: date | None = None) -> pd.DataFrame:
    """Her çiftin `as_of` gününe kadarki son dolu korelasyonundan simetrik kare matris (köşegen 1.0).

    Ortak gözlemi yetmeyen çiftler NaN kalır. Isı haritası ve API için.
    """
    col = f"corr_{window}d"
    _require(correlations, {"instrument_a", "instrument_b", "price_date", col})
    df = correlations[correlations[col].notna()]
    if as_of is not None:
        df = df[[_as_date(d) <= as_of for d in df["price_date"]]]
    codes = sorted({*correlations["instrument_a"], *correlations["instrument_b"]})
    matrix = pd.DataFrame(math.nan, index=codes, columns=codes, dtype="float64")
    for code in codes:
        matrix.loc[code, code] = 1.0
    latest = df.sort_values("price_date").groupby(["instrument_a", "instrument_b"]).tail(1)
    for row in latest.itertuples(index=False):
        value = getattr(row, col)
        matrix.loc[row.instrument_a, row.instrument_b] = value
        matrix.loc[row.instrument_b, row.instrument_a] = value
    return matrix
