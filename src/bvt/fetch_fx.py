"""Döviz çekici: USD/TRY ve EUR/TRY günlük kurlarını çekip raw.fx_rates tablosuna yükler.

Kaynaklar:
- yfinance (varsayılan, anahtarsız): USDTRY=X, EURTRY=X günlük OHLC.
- TCMB EVDS (opsiyonel): EVDS_API_KEY doluysa gösterge döviz alış kuru ikinci kaynak olarak eklenir.

Kullanım:
    python -m bvt.fetch_fx                     # son 30 gün
    python -m bvt.fetch_fx --start 2020-01-01  # geçmiş yükleme
    python -m bvt.fetch_fx --days 7 --dry-run  # DB'ye yazmadan önizleme
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

from bvt import config
from bvt.retry import retry

log = logging.getLogger("bvt.fetch_fx")

COLUMNS = ["pair", "rate_date", "source", "open", "high", "low", "close"]

# EVDS seri kodları: gösterge döviz alış kuru
EVDS_SERIES = {"USDTRY": "TP.DK.USD.A", "EURTRY": "TP.DK.EUR.A"}
EVDS_DEFAULT_URL = "https://evds2.tcmb.gov.tr/service/evds/"


class FetchError(RuntimeError):
    """Kaynaktan beklenen veri gelmediğinde fırlatılır (retry'ı tetikler)."""


# --------------------------------------------------------------------------- yfinance
@retry(attempts=4, base_delay=2.0, exceptions=(FetchError, requests.RequestException, ConnectionError))
def _download_yf(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    # Tek istekte toplu indirme (rate limit dostu). yfinance'te `end` hariçtir, bu yüzden +1 gün.
    raw = yf.download(
        tickers,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise FetchError(f"yfinance boş veri döndü: {tickers}")
    return raw


def parse_yf_frame(raw: pd.DataFrame, pairs: dict[str, str]) -> pd.DataFrame:
    """yf.download çıktısını (ticker, alan) MultiIndex sütunlarından uzun biçime çevirir."""
    frames = []
    for pair, ticker in pairs.items():
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                log.warning("%s (%s) yanıtta yok", pair, ticker)
                continue
            sub = raw[ticker]
        else:  # tek ticker, düz sütunlar
            sub = raw
        sub = sub.rename(columns=str.lower)
        out = pd.DataFrame(
            {
                "pair": pair,
                "rate_date": pd.to_datetime(sub.index).date,
                "source": "yfinance",
                "open": sub.get("open"),
                "high": sub.get("high"),
                "low": sub.get("low"),
                "close": sub.get("close"),
            }
        )
        frames.append(out.reset_index(drop=True))
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)[COLUMNS]


def fetch_yfinance(start: date, end: date, pairs: dict[str, str] | None = None) -> pd.DataFrame:
    pairs = pairs or config.FX_PAIRS
    df = clean(parse_yf_frame(_download_yf(list(pairs.values()), start, end), pairs))
    missing = set(pairs) - set(df["pair"])
    if missing:
        # Hafta sonu/tatil aralığında boş gelmesi normal; tamamen boşsa yine de uyar.
        log.warning("yfinance: %s için %s..%s aralığında veri yok", sorted(missing), start, end)
    return df


# --------------------------------------------------------------------------- EVDS
@retry(attempts=3, base_delay=2.0, exceptions=(requests.RequestException, FetchError))
def _get_evds(url: str, api_key: str) -> dict:
    # EVDS anahtarı URL'de değil, `key` başlığında gönderilir (anahtar loglara/URL'lere sızmasın).
    resp = requests.get(url, headers={"key": api_key}, timeout=30)
    if resp.status_code == 429 or resp.status_code >= 500:
        raise FetchError(f"EVDS HTTP {resp.status_code}")
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:  # anahtar hatalıysa EVDS HTML döndürebilir
        raise FetchError("EVDS JSON olmayan yanıt döndü (API anahtarını kontrol edin)") from exc


def parse_evds(payload: dict, series: dict[str, str] = EVDS_SERIES) -> pd.DataFrame:
    rows = []
    for item in payload.get("items", []):
        rate_date = pd.to_datetime(item.get("Tarih"), format="%d-%m-%Y", errors="coerce")
        if pd.isna(rate_date):
            continue
        for pair, code in series.items():
            value = item.get(code.replace(".", "_"))
            if value in (None, "", "null"):
                continue  # hafta sonu / resmi tatil
            rows.append({"pair": pair, "rate_date": rate_date.date(), "source": "evds",
                         "open": None, "high": None, "low": None, "close": float(value)})
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch_evds(start: date, end: date, api_key: str) -> pd.DataFrame:
    base = os.getenv("EVDS_BASE_URL", EVDS_DEFAULT_URL).rstrip("/") + "/"
    url = (f"{base}series={'-'.join(EVDS_SERIES.values())}"
           f"&startDate={start:%d-%m-%Y}&endDate={end:%d-%m-%Y}&type=json")
    return clean(parse_evds(_get_evds(url, api_key)))


# --------------------------------------------------------------------------- doğrulama
def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Geçersiz satırları atar: boş/pozitif olmayan kapanış, tekrar eden (pair, tarih, kaynak)."""
    if df.empty:
        return df
    df = df.copy()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce").round(6)
    bad = df["close"].isna() | (df["close"] <= 0)
    if bad.any():
        log.info("%d geçersiz satır atıldı (boş/negatif kapanış)", int(bad.sum()))
    df = df[~bad]
    inverted = df["high"].notna() & df["low"].notna() & (df["high"] < df["low"])
    if inverted.any():
        log.warning("%d satırda high < low; OHLC alanları boşaltıldı", int(inverted.sum()))
        df.loc[inverted, ["open", "high", "low"]] = None
    df = df.drop_duplicates(subset=["pair", "rate_date", "source"], keep="last")
    df = df.astype(object).where(df.notna(), None)  # NaN -> None (SQL NULL)
    return df.sort_values(["source", "pair", "rate_date"]).reset_index(drop=True)


# --------------------------------------------------------------------------- yükleme
UPSERT_SQL = """
INSERT INTO raw.fx_rates (pair, rate_date, source, open, high, low, close)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (pair, rate_date, source) DO UPDATE
SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, fetched_at = now()
"""


def load(conn, df: pd.DataFrame) -> int:
    """Satırları raw.fx_rates'e upsert eder; aynı gün tekrar çalışırsa kopya oluşmaz."""
    if df.empty:
        return 0
    rows = [tuple(r) for r in df[COLUMNS].itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- orkestrasyon
def fetch_all(start: date, end: date, evds_key: str | None = None) -> pd.DataFrame:
    """yfinance zorunlu, EVDS opsiyonel. EVDS hatası çalışmayı düşürmez, sadece loglanır."""
    if start > end:
        raise ValueError(f"start ({start}) end'den ({end}) sonra olamaz")
    frames = [fetch_yfinance(start, end)]
    if evds_key:
        try:
            frames.append(fetch_evds(start, end, evds_key))
        except Exception as exc:  # noqa: BLE001 - opsiyonel kaynak
            log.warning("EVDS atlandı: %s", exc)
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def run(start: date, end: date, dry_run: bool = False) -> int:
    df = fetch_all(start, end, os.getenv("EVDS_API_KEY") or None)
    log.info("%d satır çekildi (%s..%s): %s", len(df), start, end,
             df.groupby(["source", "pair"]).size().to_dict() if not df.empty else {})
    if dry_run:
        print(df.to_string(index=False))
        return len(df)

    from bvt.db import connect, finish_run, start_run

    with connect() as conn:
        run_id = start_run(conn, "fx")
        try:
            n = load(conn, df)
        except Exception as exc:
            conn.rollback()
            finish_run(conn, run_id, "failed", error=str(exc)[:1000])
            raise
        finish_run(conn, run_id, "success", rows_loaded=n)
    log.info("raw.fx_rates: %d satır upsert edildi", n)
    return n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="USD/TRY ve EUR/TRY günlük kurlarını çeker.")
    p.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD (varsayılan: bugün - days)")
    p.add_argument("--end", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (dahil)")
    p.add_argument("--days", type=int, default=30, help="--start verilmezse geriye kaç gün")
    p.add_argument("--dry-run", action="store_true", help="DB'ye yazma, sadece yazdır")
    args = p.parse_args(argv)
    if args.start is None:
        args.start = args.end - timedelta(days=args.days)
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    try:
        run(args.start, args.end, args.dry_run)
    except Exception as exc:  # noqa: BLE001
        log.error("Döviz çekimi başarısız: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
