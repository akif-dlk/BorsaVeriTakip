"""BIST hisse çekici: config.BIST_TICKERS + BIST 100 (XU100.IS) günlük fiyatlarını raw.stock_prices'a yükler.

Kaynak: yfinance (anahtarsız, ~15 dk gecikmeli). Tüm semboller tek istekte toplu indirilir. Uzun geçmiş
yüklemelerinde (backfill) aralık parçalara bölünür; her parça ayrı istekle çekilip hemen yüklenir, parçalar
arasında kısa bekleme yapılır (rate limit dostu, yarıda kesilirse yüklenen kısım kaybolmaz).

Kullanım:
    python -m bvt.fetch_stocks                          # son 30 gün
    python -m bvt.fetch_stocks --start 2018-01-01       # geçmiş yükleme (parça parça)
    python -m bvt.fetch_stocks --symbols THYAO GARAN    # yalnızca seçili semboller
    python -m bvt.fetch_stocks --days 7 --dry-run       # DB'ye yazmadan önizleme
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterator
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

from bvt import config
from bvt.retry import retry

log = logging.getLogger("bvt.fetch_stocks")

COLUMNS = ["symbol", "price_date", "source", "open", "high", "low", "close", "adj_close", "volume"]
PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close"]

# Backfill parça boyutu ve parçalar arası bekleme (sn)
CHUNK_DAYS = 365
CHUNK_PAUSE = 1.5


class FetchError(RuntimeError):
    """Kaynaktan beklenen veri gelmediğinde fırlatılır (retry'ı tetikler)."""


def symbol_of(ticker: str) -> str:
    """`THYAO.IS` -> `THYAO` (raw tablodaki sembol biçimi)."""
    return ticker.upper().removesuffix(".IS")


def default_tickers() -> dict[str, str]:
    """İzlenen tüm BIST sembolleri: {sembol: yfinance ticker}, endeks dahil."""
    tickers = [*config.BIST_TICKERS, config.BIST_INDEX]
    return {symbol_of(t): t for t in tickers}


def select_tickers(symbols: list[str] | None) -> dict[str, str]:
    """CLI'dan gelen sembolleri doğrular. Bilinmeyen sembol varsa ValueError."""
    universe = default_tickers()
    if not symbols:
        return universe
    wanted = [symbol_of(s) for s in symbols]
    unknown = sorted(set(wanted) - set(universe))
    if unknown:
        raise ValueError(f"Bilinmeyen sembol(ler): {unknown}. Geçerli: {sorted(universe)}")
    return {s: universe[s] for s in wanted}


# --------------------------------------------------------------------------- yfinance
@retry(attempts=4, base_delay=2.0, exceptions=(FetchError, requests.RequestException, ConnectionError))
def _download_yf(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    # yfinance'te `end` hariçtir, bu yüzden +1 gün.
    raw = yf.download(
        tickers,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,  # hem ham close hem adj_close gelsin
        actions=False,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise FetchError(f"yfinance boş veri döndü: {tickers}")
    return raw


def parse_yf_frame(raw: pd.DataFrame, tickers: dict[str, str]) -> pd.DataFrame:
    """yf.download çıktısını (ticker, alan) MultiIndex sütunlarından uzun biçime çevirir."""
    frames = []
    for symbol, ticker in tickers.items():
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                log.warning("%s (%s) yanıtta yok", symbol, ticker)
                continue
            sub = raw[ticker]
        else:  # tek ticker, düz sütunlar
            sub = raw
        sub = sub.rename(columns=lambda c: str(c).lower().replace(" ", "_"))
        out = pd.DataFrame(
            {
                "symbol": symbol,
                "price_date": pd.to_datetime(sub.index).date,
                "source": "yfinance",
                "open": sub.get("open"),
                "high": sub.get("high"),
                "low": sub.get("low"),
                "close": sub.get("close"),
                "adj_close": sub.get("adj_close"),
                "volume": sub.get("volume"),
            }
        )
        frames.append(out.reset_index(drop=True))
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)[COLUMNS]


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Geçersiz satırları atar ve tipleri DB'ye hazırlar.

    - Boş/pozitif olmayan kapanış: satır atılır (yfinance işlem olmayan günlerde NaN satır döndürebilir).
    - high < low: OHLC alanları boşaltılır, kapanış korunur.
    - Negatif hacim: NULL yapılır.
    - Tekrar eden (symbol, tarih, kaynak): sonuncusu kalır.
    """
    if df.empty:
        return df
    df = df.copy()
    for col in PRICE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(6)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    bad = df["close"].isna() | (df["close"] <= 0)
    if bad.any():
        log.info("%d geçersiz satır atıldı (boş/negatif kapanış)", int(bad.sum()))
    df = df[~bad]
    inverted = df["high"].notna() & df["low"].notna() & (df["high"] < df["low"])
    if inverted.any():
        log.warning("%d satırda high < low; OHLC alanları boşaltıldı", int(inverted.sum()))
        df.loc[inverted, ["open", "high", "low"]] = None
    neg_vol = df["volume"] < 0
    if neg_vol.any():
        log.warning("%d satırda negatif hacim; NULL yapıldı", int(neg_vol.sum()))
        df.loc[neg_vol, "volume"] = None
    df = df.drop_duplicates(subset=["symbol", "price_date", "source"], keep="last")
    df = df.astype(object).where(df.notna(), None)  # NaN -> None (SQL NULL)
    # BIGINT için Python int; object dtype korunmazsa pandas None'ı tekrar NaN'a çevirir.
    df["volume"] = pd.Series([None if v is None else int(v) for v in df["volume"]], index=df.index, dtype=object)
    return df.sort_values(["source", "symbol", "price_date"]).reset_index(drop=True)


def fetch_range(start: date, end: date, tickers: dict[str, str] | None = None) -> pd.DataFrame:
    """Tek toplu istekle [start, end] aralığını çeker ve temizler."""
    tickers = tickers or default_tickers()
    df = clean(parse_yf_frame(_download_yf(list(tickers.values()), start, end), tickers))
    missing = set(tickers) - set(df["symbol"]) if not df.empty else set(tickers)
    if missing:
        log.warning("yfinance: %s için %s..%s aralığında veri yok", sorted(missing), start, end)
    return df


def chunk_ranges(start: date, end: date, chunk_days: int = CHUNK_DAYS) -> Iterator[tuple[date, date]]:
    """[start, end] aralığını en fazla `chunk_days` günlük, örtüşmeyen parçalara böler (uçlar dahil)."""
    if start > end:
        raise ValueError(f"start ({start}) end'den ({end}) sonra olamaz")
    if chunk_days < 1:
        raise ValueError("chunk_days >= 1 olmalı")
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=chunk_days - 1))
        yield cur, stop
        cur = stop + timedelta(days=1)


# --------------------------------------------------------------------------- yükleme
UPSERT_SQL = """
INSERT INTO raw.stock_prices (symbol, price_date, source, open, high, low, close, adj_close, volume)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, price_date, source) DO UPDATE
SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
    adj_close = EXCLUDED.adj_close, volume = EXCLUDED.volume, fetched_at = now()
"""


def load(conn, df: pd.DataFrame) -> int:
    """Satırları raw.stock_prices'a upsert eder; aynı gün tekrar çalışırsa kopya oluşmaz."""
    if df.empty:
        return 0
    rows = [tuple(r) for r in df[COLUMNS].itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- orkestrasyon
def iter_chunks(start: date, end: date, tickers: dict[str, str], chunk_days: int = CHUNK_DAYS,
                pause: float = CHUNK_PAUSE, sleep=time.sleep) -> Iterator[pd.DataFrame]:
    """Her parçayı çekip temiz DataFrame olarak verir. Tamamen boş parça (ör. tatil haftası) atlanır."""
    for i, (s, e) in enumerate(chunk_ranges(start, end, chunk_days)):
        if i and pause:
            sleep(pause)
        try:
            df = fetch_range(s, e, tickers)
        except FetchError as exc:
            # Kısa aralıkta (hafta sonu/tatil) hiç işlem olmaması normaldir; uzun aralıkta ise gerçek hata.
            if (e - s).days < 7:
                log.info("%s..%s: işlem günü yok, atlandı (%s)", s, e, exc)
                continue
            raise
        log.info("%s..%s: %d satır", s, e, len(df))
        yield df


def run(start: date, end: date, symbols: list[str] | None = None, dry_run: bool = False,
        chunk_days: int = CHUNK_DAYS) -> int:
    tickers = select_tickers(symbols)
    if dry_run:
        frames = [df for df in iter_chunks(start, end, tickers, chunk_days) if not df.empty]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        print(df.to_string(index=False))
        log.info("%d satır (dry-run): %s", len(df), df.groupby("symbol").size().to_dict() if len(df) else {})
        return len(df)

    from bvt.db import connect, finish_run, start_run

    total = 0
    with connect() as conn:
        run_id = start_run(conn, "stocks")
        try:
            for df in iter_chunks(start, end, tickers, chunk_days):
                total += load(conn, df)
        except Exception as exc:
            conn.rollback()
            finish_run(conn, run_id, "failed", rows_loaded=total, error=str(exc)[:1000])
            raise
        finish_run(conn, run_id, "success", rows_loaded=total)
    log.info("raw.stock_prices: %d satır upsert edildi (%d sembol)", total, len(tickers))
    return total


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BIST hisseleri ve BIST 100 günlük fiyatlarını çeker.")
    p.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD (varsayılan: bugün - days)")
    p.add_argument("--end", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (dahil)")
    p.add_argument("--days", type=int, default=30, help="--start verilmezse geriye kaç gün")
    p.add_argument("--symbols", nargs="+", help="Yalnızca bu semboller (ör. THYAO XU100); varsayılan: hepsi")
    p.add_argument("--chunk-days", type=int, default=CHUNK_DAYS, help="Geçmiş yüklemede istek başına gün sayısı")
    p.add_argument("--dry-run", action="store_true", help="DB'ye yazma, sadece yazdır")
    args = p.parse_args(argv)
    if args.start is None:
        args.start = args.end - timedelta(days=args.days)
    if args.start > args.end:
        p.error(f"--start ({args.start}) --end'den ({args.end}) sonra olamaz")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    try:
        run(args.start, args.end, args.symbols, args.dry_run, args.chunk_days)
    except Exception as exc:  # noqa: BLE001
        log.error("Hisse çekimi başarısız: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
