"""Hafta sonu / tatil boşluklarını gerçek veri eksiklerinden ayırır.

Raw katmanda yalnızca işlem günleri vardır; hafta sonu ve resmî tatillerde satır oluşmaz. Her piyasanın tatil
takvimi farklıdır (BIST: Türkiye tatilleri ve dini bayramlar, GC=F: ABD tatilleri, döviz: yalnızca 25 Aralık /
1 Ocak gibi küresel kapanışlar) ve dini bayram tarihleri her yıl değişir. Elle tutulan bir tatil listesi yerine
veriye dayalı sınıflandırma yapılır:

- Hafta sonları hiç değerlendirilmez.
- Bir iş gününde kaynağın (fx / stocks / gold) **hiçbir** enstrümanında veri yoksa: `market_closed`
  (piyasa kapalı / tatil). Boşluk değil, takvim bilgisidir.
- Aynı gün diğer enstrümanlarda veri var ama bu enstrümanda yoksa: `missing` (gerçek eksik, onarılmalı).
- Bir enstrümanın ilk görüldüğü tarihten önceki günler eksik sayılmaz (sonradan eklenen sembol).

Sonuç `ops.data_gaps` tablosuna anlık görüntü olarak yazılır; `missing` boşluklar
`python -m bvt.backfill --repair-gaps` ile yeniden çekilir. Fiyat serilerindeki tatil boşluklarının ileri
doldurulması (takvim hizalama) dbt katmanında, `dim_date` ile yapılır.

Kullanım:
    python -m bvt.gaps                       # üç kaynak, son 30 gün, ops.data_gaps'e yazar
    python -m bvt.gaps --source stocks --days 365 --no-save
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from bvt import config

log = logging.getLogger("bvt.gaps")

MARKET_CLOSED = "market_closed"
MISSING = "missing"
ALL_INSTRUMENTS = "*"
DEFAULT_CHECK_DAYS = 30

# Kaynak -> (pencere içindeki gözlenen günler, enstrüman başına ilk görülen gün). Her ikisi de (enstrüman, tarih).
# FX'te yalnızca yfinance dikkate alınır: EVDS opsiyoneldir ve kendi takvimi (TCMB) vardır.
SOURCE_QUERIES: dict[str, tuple[str, str]] = {
    "fx": (
        "SELECT DISTINCT pair, rate_date FROM raw.fx_rates WHERE source = 'yfinance' AND rate_date BETWEEN %s AND %s",
        "SELECT pair, min(rate_date) FROM raw.fx_rates WHERE source = 'yfinance' GROUP BY pair",
    ),
    "stocks": (
        "SELECT DISTINCT symbol, price_date FROM raw.stock_prices WHERE source = 'yfinance' "
        "AND price_date BETWEEN %s AND %s",
        "SELECT symbol, min(price_date) FROM raw.stock_prices WHERE source = 'yfinance' GROUP BY symbol",
    ),
    "gold": (
        "SELECT DISTINCT instrument, price_date FROM raw.gold_prices WHERE price_date BETWEEN %s AND %s",
        "SELECT instrument, min(price_date) FROM raw.gold_prices GROUP BY instrument",
    ),
}


@dataclass(frozen=True, order=True)
class Gap:
    source: str
    gap_date: date
    instrument: str
    kind: str


def expected_instruments(source: str) -> list[str]:
    """Kaynağın izlenen enstrümanları (raw tablolardaki adlarıyla)."""
    if source == "fx":
        return list(config.FX_PAIRS)
    if source == "stocks":
        return config.bist_symbols()
    if source == "gold":
        return list(config.GOLD_INSTRUMENTS)
    raise ValueError(f"Bilinmeyen kaynak: {source!r}. Geçerli: {sorted(SOURCE_QUERIES)}")


def weekdays(start: date, end: date) -> list[date]:
    """[start, end] aralığındaki Pzt-Cum günleri (uçlar dahil)."""
    if start > end:
        raise ValueError(f"start ({start}) end'den ({end}) sonra olamaz")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


def find_gaps(source: str, observed: dict[str, set[date]], first_seen: dict[str, date], start: date, end: date,
              instruments: Iterable[str] | None = None) -> list[Gap]:
    """Pencerede iş günü boşluklarını bulur ve `market_closed` / `missing` olarak sınıflandırır (saf fonksiyon)."""
    instruments = list(instruments if instruments is not None else expected_instruments(source))
    never = [i for i in instruments if i not in first_seen]
    if never:
        log.warning("%s: %s için hiç veri yok; önce geçmiş yükleme gerekir (python -m bvt.backfill)", source, never)
    active = {i: first_seen[i] for i in instruments if i in first_seen}

    gaps: list[Gap] = []
    for day in weekdays(start, end):
        live = [i for i, first in active.items() if first <= day]
        if not live:
            continue
        have = [i for i in live if day in observed.get(i, ())]
        if not have:
            gaps.append(Gap(source, day, ALL_INSTRUMENTS, MARKET_CLOSED))
        else:
            gaps.extend(Gap(source, day, i, MISSING) for i in live if i not in have)
    return gaps


def merge_windows(days: Iterable[date], max_gap_days: int = 7) -> list[tuple[date, date]]:
    """Tarihleri, aralarında en fazla `max_gap_days` gün olan ardışık pencerelere birleştirir.

    Onarımda her eksik gün için ayrı istek atmak yerine yakın günler tek istekte çekilir (rate limit dostu).
    """
    windows: list[list[date]] = []
    for day in sorted(set(days)):
        if windows and (day - windows[-1][1]).days <= max_gap_days:
            windows[-1][1] = day
        else:
            windows.append([day, day])
    return [(s, e) for s, e in windows]


def summarize(gaps: Iterable[Gap]) -> dict:
    """XCom / log için küçük özet: tür başına sayı ve eksik günü olan enstrümanlar."""
    gaps = list(gaps)
    kinds = Counter(g.kind for g in gaps)
    missing = Counter(g.instrument for g in gaps if g.kind == MISSING)
    return {MISSING: kinds.get(MISSING, 0), MARKET_CLOSED: kinds.get(MARKET_CLOSED, 0),
            "missing_by_instrument": dict(sorted(missing.items()))}


# --------------------------------------------------------------------------- veritabanı
def observed_dates(conn, source: str, start: date, end: date) -> tuple[dict[str, set[date]], dict[str, date]]:
    window_sql, first_sql = SOURCE_QUERIES[source]
    observed: dict[str, set[date]] = {}
    for instrument, day in conn.execute(window_sql, (start, end)).fetchall():
        observed.setdefault(instrument, set()).add(day)
    first_seen = {instrument: day for instrument, day in conn.execute(first_sql).fetchall()}
    return observed, first_seen


def save_gaps(conn, source: str, start: date, end: date, gaps: list[Gap]) -> None:
    """Kaynak + pencere için ops.data_gaps anlık görüntüsünü yeniden yazar (tek transaction)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ops.data_gaps WHERE source = %s AND gap_date BETWEEN %s AND %s",
                    (source, start, end))
        if gaps:
            cur.executemany(
                "INSERT INTO ops.data_gaps (source, instrument, gap_date, kind) VALUES (%s, %s, %s, %s)",
                [(g.source, g.instrument, g.gap_date, g.kind) for g in gaps],
            )
    conn.commit()


def check_source(conn, source: str, start: date, end: date, save: bool = True) -> list[Gap]:
    observed, first_seen = observed_dates(conn, source, start, end)
    gaps = find_gaps(source, observed, first_seen, start, end)
    if save:
        save_gaps(conn, source, start, end, gaps)
    s = summarize(gaps)
    level = logging.WARNING if s[MISSING] else logging.INFO
    log.log(level, "%s %s..%s: %d eksik gün, %d piyasa kapalı gün %s", source, start, end, s[MISSING],
            s[MARKET_CLOSED], s["missing_by_instrument"] or "",
            extra={"source": source, "missing": s[MISSING], "market_closed": s[MARKET_CLOSED]})
    return gaps


def check(sources: Iterable[str], start: date, end: date, save: bool = True) -> dict[str, list[Gap]]:
    from bvt.db import connect

    with connect() as conn:
        return {source: check_source(conn, source, start, end, save) for source in sources}


# --------------------------------------------------------------------------- CLI
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Raw tablolarda tatil boşluklarını ve gerçek eksikleri raporlar.")
    p.add_argument("--source", nargs="+", choices=sorted(SOURCE_QUERIES), default=list(SOURCE_QUERIES))
    p.add_argument("--end", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (dahil)")
    p.add_argument("--days", type=int, default=DEFAULT_CHECK_DAYS, help="Geriye kaç gün kontrol edilir")
    p.add_argument("--no-save", action="store_true", help="ops.data_gaps'e yazma, yalnızca raporla")
    p.add_argument("--show-closed", action="store_true", help="Piyasa kapalı günleri de listele")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from bvt.logging_setup import setup_logging

    setup_logging()
    args = parse_args(argv)
    start = args.end - timedelta(days=args.days)
    try:
        result = check(args.source, start, args.end, save=not args.no_save)
    except Exception as exc:  # noqa: BLE001
        log.error("Boşluk kontrolü başarısız: %s", exc)
        return 1
    for gaps in result.values():
        for g in sorted(gaps):
            if g.kind == MISSING or args.show_closed:
                print(f"{g.source:<7} {g.gap_date} {g.instrument:<12} {g.kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
