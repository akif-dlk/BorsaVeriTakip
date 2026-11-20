"""Altın çekici: ons altın (GC=F, USD) çekip USD/TRY ile gram altını (TRY) hesaplar, raw.gold_prices'a yükler.

Gram altın = ons × USDTRY / 31.1034768 (1 troy ons = 31.1034768 g).

- Ons altın ve USD/TRY aynı toplu istekte çekilir (rate limit dostu, tek istek).
- GC=F ABD vadeli piyasası takvimine, USDTRY=X ise döviz piyasası takvimine göre işler; tarihler birebir
  örtüşmeyebilir. Her ons satırı için o günkü (yoksa en fazla FX_MAX_STALE_DAYS gün önceki) son USD/TRY
  kapanışı kullanılır. Bu nedenle istenen aralıktan FX_LOOKBACK_DAYS gün önceden itibaren çekim yapılır.
- Gram OHLC alanları onsun OHLC değerleri × aynı günün USD/TRY kapanışıyla hesaplanır (yaklaşık değer;
  gün içi kur hareketi yok sayılır). Kullanılan kur `usdtry` sütununda saklanır.
- Uzun geçmiş yüklemeleri BIST çekicisindeki gibi parçalara bölünür.

Kullanım:
    python -m bvt.fetch_gold                     # son 30 gün
    python -m bvt.fetch_gold --start 2015-01-01  # geçmiş yükleme (parça parça)
    python -m bvt.fetch_gold --days 7 --dry-run  # DB'ye yazmadan önizleme
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterator
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from bvt import config
from bvt.fetch_stocks import chunk_ranges
from bvt.raw_loader import GOLD_PRICES, LoadResult, load_frame
from bvt.logging_setup import setup_logging
from bvt.retry import TRANSIENT_ERRORS, retry

log = logging.getLogger("bvt.fetch_gold")

COLUMNS = list(GOLD_PRICES.columns)
OHLC = ["open", "high", "low", "close"]

OUNCE, GRAM = config.GOLD_INSTRUMENTS  # XAUUSD_OZ, XAUTRY_GRAM
USDTRY_TICKER = config.FX_PAIRS["USDTRY"]

# Ons tarihi için en fazla kaç gün eski kur kabul edilir (uzun tatil + hafta sonu)
FX_MAX_STALE_DAYS = 4
# Aralık başındaki ons satırları için kur bulunabilsin diye geriye ek çekim
FX_LOOKBACK_DAYS = 7

CHUNK_DAYS = 365
CHUNK_PAUSE = 1.5


class FetchError(RuntimeError):
    """Kaynaktan beklenen veri gelmediğinde fırlatılır (retry'ı tetikler)."""


# --------------------------------------------------------------------------- yfinance
@retry(attempts=4, base_delay=2.0, exceptions=(FetchError, *TRANSIENT_ERRORS))
def _download_yf(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    # yfinance'te `end` hariçtir, bu yüzden +1 gün.
    raw = yf.download(
        tickers,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise FetchError(f"yfinance boş veri döndü: {tickers}")
    return raw


def extract_ticker(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Toplu yanıttan tek ticker'ın OHLC'sini tarih (tz'siz, gün başı) indeksli olarak çıkarır.

    Ticker yanıtta yoksa veya hiç geçerli kapanışı yoksa boş DataFrame döner.
    """
    if isinstance(raw.columns, pd.MultiIndex):
        if ticker not in raw.columns.get_level_values(0):
            return pd.DataFrame(columns=OHLC, index=pd.DatetimeIndex([]), dtype=float)
        sub = raw[ticker]
    else:  # tek ticker, düz sütunlar
        sub = raw
    sub = sub.rename(columns=lambda c: str(c).lower().replace(" ", "_"))
    out = pd.DataFrame({c: pd.to_numeric(sub[c], errors="coerce") if c in sub else float("nan") for c in OHLC},
                       index=sub.index)
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out = out[out["close"].notna() & (out["close"] > 0)]
    return out[~out.index.duplicated(keep="last")].sort_index()


def gram_from_ounce(ounce: pd.DataFrame, usdtry: pd.Series, max_stale_days: int = FX_MAX_STALE_DAYS) -> pd.DataFrame:
    """Ons OHLC (USD) + USD/TRY kapanış serisinden gram altın OHLC (TRY) üretir.

    Her ons tarihi için o tarihteki ya da en fazla `max_stale_days` gün önceki son kur kullanılır; uygun kur
    yoksa o gün için gram satırı üretilmez. Dönen DataFrame: indeks tarih, sütunlar OHLC + `usdtry`.
    """
    if ounce.empty or usdtry.empty:
        return pd.DataFrame(columns=[*OHLC, "usdtry"])
    left = ounce.rename_axis("dt").reset_index()
    right = usdtry.dropna().sort_index().rename("usdtry").rename_axis("dt").reset_index()
    merged = pd.merge_asof(left.sort_values("dt"), right, on="dt", direction="backward",
                           tolerance=pd.Timedelta(days=max_stale_days))
    missing = merged["usdtry"].isna()
    if missing.any():
        log.warning("%d ons günü için güncel USD/TRY yok; gram altın hesaplanmadı: %s", int(missing.sum()),
                    [d.date().isoformat() for d in merged.loc[missing, "dt"]][:5])
    merged = merged[~missing].set_index("dt")
    factor = merged["usdtry"] / config.TROY_OUNCE_GRAMS
    gram = merged[OHLC].mul(factor, axis=0)
    gram["usdtry"] = merged["usdtry"]
    return gram


def build_rows(ounce: pd.DataFrame, usdtry: pd.Series, start: date, end: date) -> pd.DataFrame:
    """Ons ve gram satırlarını raw.gold_prices biçiminde (uzun) birleştirir, [start, end] dışını atar."""
    gram = gram_from_ounce(ounce, usdtry)

    def to_long(frame: pd.DataFrame, instrument: str, source: str, currency: str) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=COLUMNS)
        return pd.DataFrame(
            {
                "instrument": instrument,
                "price_date": frame.index.date,
                "source": source,
                "currency": currency,
                **{c: frame[c].to_numpy(dtype=float) for c in OHLC},
                # Ons satırlarında kur yok; tüm-NaN sütun concat uyarısı vermesin diye object dtype
                "usdtry": frame["usdtry"].to_numpy(dtype=float) if "usdtry" in frame
                else pd.Series([None] * len(frame), dtype=object),
            }
        )

    frames = [f for f in (to_long(ounce, OUNCE, "yfinance", "USD"), to_long(gram, GRAM, "derived", "TRY"))
              if not f.empty]
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat([f.astype({"usdtry": object}) for f in frames], ignore_index=True)[COLUMNS]
    return df[(df["price_date"] >= start) & (df["price_date"] <= end)].reset_index(drop=True)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Geçersiz satırları atar ve tipleri DB'ye hazırlar (döviz/hisse çekicileriyle aynı kurallar).

    - Boş/pozitif olmayan kapanış: satır atılır.
    - high < low: OHLC alanları boşaltılır, kapanış korunur.
    - Tekrar eden (enstrüman, tarih, kaynak): sonuncusu kalır.
    """
    if df.empty:
        return df
    df = df.copy()
    for col in [*OHLC, "usdtry"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(6)
    bad = df["close"].isna() | (df["close"] <= 0)
    if bad.any():
        log.info("%d geçersiz satır atıldı (boş/negatif kapanış)", int(bad.sum()))
    df = df[~bad]
    inverted = df["high"].notna() & df["low"].notna() & (df["high"] < df["low"])
    if inverted.any():
        log.warning("%d satırda high < low; OHLC alanları boşaltıldı", int(inverted.sum()))
        df.loc[inverted, ["open", "high", "low"]] = None
    df = df.drop_duplicates(subset=["instrument", "price_date", "source"], keep="last")
    df = df.astype(object).where(df.notna(), None)  # NaN -> None (SQL NULL)
    return df.sort_values(["instrument", "price_date"]).reset_index(drop=True)


def fetch_range(start: date, end: date) -> pd.DataFrame:
    """[start, end] için ons + gram altını tek toplu istekle çekip temizler.

    Ons altın yoksa FetchError (retry/atlama kararı çağırana kalır). USD/TRY yoksa yalnızca ons satırları döner.
    """
    if start > end:
        raise ValueError(f"start ({start}) end'den ({end}) sonra olamaz")
    raw = _download_yf([config.GOLD_OUNCE, USDTRY_TICKER], start - timedelta(days=FX_LOOKBACK_DAYS), end)
    ounce = extract_ticker(raw, config.GOLD_OUNCE)
    ounce = ounce[(ounce.index >= pd.Timestamp(start)) & (ounce.index <= pd.Timestamp(end))]
    if ounce.empty:
        raise FetchError(f"{config.GOLD_OUNCE} için {start}..{end} aralığında veri yok")
    usdtry = extract_ticker(raw, USDTRY_TICKER)["close"]
    if usdtry.empty:
        log.warning("%s yanıtta yok; gram altın bu çalışmada hesaplanmadı", USDTRY_TICKER)
    return clean(build_rows(ounce, usdtry, start, end))


# --------------------------------------------------------------------------- yükleme
def load(conn, df: pd.DataFrame) -> LoadResult:
    """Satırları raw.gold_prices'a idempotent upsert eder (bkz. bvt.raw_loader); tekrar çalışmada kopya oluşmaz."""
    return load_frame(conn, GOLD_PRICES, df)


# --------------------------------------------------------------------------- orkestrasyon
def iter_chunks(start: date, end: date, chunk_days: int = CHUNK_DAYS, pause: float = CHUNK_PAUSE,
                sleep=time.sleep) -> Iterator[pd.DataFrame]:
    """Her parçayı çekip temiz DataFrame olarak verir. Kısa ve tamamen boş parça (hafta sonu/tatil) atlanır."""
    for i, (s, e) in enumerate(chunk_ranges(start, end, chunk_days)):
        if i and pause:
            sleep(pause)
        try:
            df = fetch_range(s, e)
        except FetchError as exc:
            if (e - s).days < 7:
                log.info("%s..%s: işlem günü yok, atlandı (%s)", s, e, exc)
                continue
            raise
        log.info("%s..%s: %d satır", s, e, len(df))
        yield df


def run(start: date, end: date, dry_run: bool = False, chunk_days: int = CHUNK_DAYS) -> int:
    if dry_run:
        frames = [df for df in iter_chunks(start, end, chunk_days) if not df.empty]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        print(df.to_string(index=False))
        log.info("%d satır (dry-run): %s", len(df), df.groupby("instrument").size().to_dict() if len(df) else {})
        return len(df)

    from bvt.db import connect, finish_run, start_run

    total = LoadResult()
    with connect() as conn:
        run_id = start_run(conn, "gold", start, end)
        try:
            for df in iter_chunks(start, end, chunk_days):
                total += load(conn, df)
        except Exception as exc:
            conn.rollback()
            finish_run(conn, run_id, "failed", total, error=str(exc)[:1000])
            raise
        finish_run(conn, run_id, "success", total)
    log.info("raw.gold_prices: %s", total)
    return total.total


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ons altın (USD) ve gram altın (TRY) günlük fiyatlarını çeker.")
    p.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD (varsayılan: bugün - days)")
    p.add_argument("--end", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (dahil)")
    p.add_argument("--days", type=int, default=30, help="--start verilmezse geriye kaç gün")
    p.add_argument("--chunk-days", type=int, default=CHUNK_DAYS, help="Geçmiş yüklemede istek başına gün sayısı")
    p.add_argument("--dry-run", action="store_true", help="DB'ye yazma, sadece yazdır")
    args = p.parse_args(argv)
    if args.start is None:
        args.start = args.end - timedelta(days=args.days)
    if args.start > args.end:
        p.error(f"--start ({args.start}) --end'den ({args.end}) sonra olamaz")
    return args


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    try:
        run(args.start, args.end, args.dry_run, args.chunk_days)
    except Exception as exc:  # noqa: BLE001
        log.error("Altın çekimi başarısız: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
