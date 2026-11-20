"""Çekicileri tek bir giriş noktasından çalıştırır (Airflow DAG'i ve elle çalıştırma için).

Airflow görevleri yalnızca bu modülü çağırır; iş mantığı çekicilerde kalır, DAG dosyası ince kalır. Modül
üst düzeyde yalnızca standart kütüphaneyi içe aktarır: DAG ayrıştırması (scheduler her birkaç saniyede bir
yapar) pandas/yfinance yüklemez, çekiciler ancak görev çalışırken içe aktarılır.

Günlük pencere: çalışmanın `data_interval_end` anının İstanbul saatindeki tarihi pencerenin sonudur; başlangıç
`BVT_LOOKBACK_DAYS` (varsayılan 7) gün öncesidir. Yükleme idempotent olduğundan son günleri her çalışmada
yeniden çekmek kopya üretmez; kaçırılan bir günü ve kaynak revizyonlarını (ör. `adj_close`) kendiliğinden yakalar.

Kullanım:
    python -m bvt.pipeline                         # üç kaynak, bugünle biten günlük pencere
    python -m bvt.pipeline --source fx gold        # yalnızca seçili kaynaklar
    python -m bvt.pipeline --end 2026-11-13 --lookback-days 3
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("bvt.pipeline")

# Kaynak adı -> çekici modülü. Adlar ops.pipeline_runs.source ile aynıdır.
SOURCES: dict[str, str] = {"fx": "bvt.fetch_fx", "stocks": "bvt.fetch_stocks", "gold": "bvt.fetch_gold"}
MARKET_TZ = ZoneInfo("Europe/Istanbul")
DEFAULT_LOOKBACK_DAYS = 7


def lookback_days(value: int | None = None) -> int:
    """Pencere uzunluğu: parametre > BVT_LOOKBACK_DAYS > varsayılan. 0 yalnızca bitiş gününü çeker."""
    if value is None:
        raw = os.getenv("BVT_LOOKBACK_DAYS") or str(DEFAULT_LOOKBACK_DAYS)
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"BVT_LOOKBACK_DAYS tam sayı olmalı: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"lookback_days negatif olamaz: {value}")
    return value


def market_date(moment: datetime | date) -> date:
    """Bir anı İstanbul saatindeki takvim gününe çevirir. Saat dilimsiz datetime UTC kabul edilir (Airflow gibi)."""
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo("UTC"))
        return moment.astimezone(MARKET_TZ).date()
    return moment


def daily_window(run_end: datetime | date, lookback: int | None = None) -> tuple[date, date]:
    """Günlük çalışmanın [başlangıç, bitiş] tarih penceresi (uçlar dahil)."""
    end = market_date(run_end)
    return end - timedelta(days=lookback_days(lookback)), end


def run_source(source: str, start: date, end: date, trigger: str = "manual") -> int:
    """Tek bir kaynağın çekicisini çalıştırır, yüklenen (tekilleştirilmiş) satır sayısını döner.

    `trigger` (manual / airflow / backfill / repair) `ops.pipeline_runs.run_trigger`'a ve JSON loglara yazılır.
    Hata yutulmaz: çekici `ops.pipeline_runs`'a 'failed' yazıp hatayı yeniden fırlatır, Airflow görevi de
    böylece başarısız sayılır (ve Airflow'un görev düzeyindeki retry'ı devreye girer).
    """
    if source not in SOURCES:
        raise ValueError(f"Bilinmeyen kaynak: {source!r}. Geçerli: {sorted(SOURCES)}")
    if start > end:
        raise ValueError(f"start ({start}) end'den ({end}) sonra olamaz")
    from bvt.run_context import run_trigger

    module = importlib.import_module(SOURCES[source])
    fields = {"source": source, "window_start": start.isoformat(), "window_end": end.isoformat()}
    log.info("%s: %s..%s çekiliyor (%s)", source, start, end, trigger, extra=fields)
    began = time.monotonic()
    with run_trigger(trigger):
        try:
            rows = module.run(start, end)
        except Exception as exc:
            log.error("%s: %s..%s başarısız (%.1f sn): %s", source, start, end, time.monotonic() - began, exc,
                      extra={**fields, "duration_s": round(time.monotonic() - began, 1)})
            raise
    duration = time.monotonic() - began
    log.info("%s: %d satır, %.1f sn", source, rows, duration, extra={**fields, "rows": rows,
                                                                      "duration_s": round(duration, 1)})
    return rows


def run_daily(source: str, run_end: datetime | date, lookback: int | None = None, trigger: str = "manual") -> dict:
    """Airflow görevinin çağırdığı fonksiyon. Dönen sözlük XCom'a yazılır (küçük ve JSON uyumlu)."""
    start, end = daily_window(run_end, lookback)
    rows = run_source(source, start, end, trigger)
    return {"source": source, "start": start.isoformat(), "end": end.isoformat(), "rows": rows}


def check_gaps(run_end: datetime | date, days: int | None = None) -> dict:
    """Son `BVT_GAP_CHECK_DAYS` (varsayılan 30) günün boşluk raporunu ops.data_gaps'e yazar, özeti döner.

    Görevi düşürmez: gerçek eksikler WARNING olarak loglanır; onarım `python -m bvt.backfill --repair-gaps`
    ile, alarm Gün 13'te eklenir.
    """
    from bvt import gaps

    if days is None:
        days = int(os.getenv("BVT_GAP_CHECK_DAYS") or gaps.DEFAULT_CHECK_DAYS)
    end = market_date(run_end)
    start = end - timedelta(days=days)
    result = gaps.check(list(SOURCES), start, end)
    return {"start": start.isoformat(), "end": end.isoformat(),
            **{source: gaps.summarize(found) for source, found in result.items()}}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Günlük pencereyle bir veya daha fazla çekiciyi çalıştırır.")
    p.add_argument("--source", nargs="+", choices=sorted(SOURCES), default=list(SOURCES),
                   help="Çalıştırılacak kaynak(lar); varsayılan: hepsi")
    p.add_argument("--end", type=date.fromisoformat, default=None,
                   help="Pencerenin son günü YYYY-MM-DD (varsayılan: bugün, İstanbul)")
    p.add_argument("--lookback-days", type=int, default=None,
                   help=f"Geriye kaç gün (varsayılan: BVT_LOOKBACK_DAYS veya {DEFAULT_LOOKBACK_DAYS})")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from bvt.logging_setup import setup_logging

    setup_logging()
    args = parse_args(argv)
    run_end = args.end or datetime.now(MARKET_TZ)
    failed = []
    for source in args.source:  # bir kaynağın hatası diğerlerini durdurmaz (DAG'deki paralel görevler gibi)
        try:
            log.info("%s", run_daily(source, run_end, args.lookback_days))
        except Exception:  # noqa: BLE001 - ayrıntı run_source'ta loglandı
            failed.append(source)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
