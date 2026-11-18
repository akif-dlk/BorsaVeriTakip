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


def run_source(source: str, start: date, end: date) -> int:
    """Tek bir kaynağın çekicisini çalıştırır, yüklenen (tekilleştirilmiş) satır sayısını döner.

    Hata yutulmaz: çekici `ops.pipeline_runs`'a 'failed' yazıp hatayı yeniden fırlatır, Airflow görevi de
    böylece başarısız sayılır.
    """
    if source not in SOURCES:
        raise ValueError(f"Bilinmeyen kaynak: {source!r}. Geçerli: {sorted(SOURCES)}")
    if start > end:
        raise ValueError(f"start ({start}) end'den ({end}) sonra olamaz")
    module = importlib.import_module(SOURCES[source])
    log.info("%s: %s..%s çekiliyor", source, start, end)
    return module.run(start, end)


def run_daily(source: str, run_end: datetime | date, lookback: int | None = None) -> dict:
    """Airflow görevinin çağırdığı fonksiyon. Dönen sözlük XCom'a yazılır (küçük ve JSON uyumlu)."""
    start, end = daily_window(run_end, lookback)
    rows = run_source(source, start, end)
    return {"source": source, "start": start.isoformat(), "end": end.isoformat(), "rows": rows}


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    run_end = args.end or datetime.now(MARKET_TZ)
    failed = []
    for source in args.source:  # bir kaynağın hatası diğerlerini durdurmaz (DAG'deki paralel görevler gibi)
        try:
            log.info("%s", run_daily(source, run_end, args.lookback_days))
        except Exception as exc:  # noqa: BLE001
            log.error("%s başarısız: %s", source, exc)
            failed.append(source)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
