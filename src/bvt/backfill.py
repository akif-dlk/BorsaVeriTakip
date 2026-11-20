"""Geçmiş yükleme (backfill) ve boşluk onarımı.

Geçmiş yükleme:
- [start, end] aralığı kaynak başına `--chunk-days` (varsayılan 365) günlük parçalara bölünür; her parça
  `bvt.pipeline.run_source` ile ayrı bir çalışma olarak yüklenir ve `ops.pipeline_runs`'a
  `run_trigger = 'backfill'` ile kaydedilir.
- **Devam ettirilebilir:** daha önce başarıyla yüklenmiş bir pencerenin kapsadığı parçalar atlanır; yarıda
  kesilen bir backfill aynı komutla kaldığı yerden sürer (`--no-resume` hepsini yeniden çeker).
- **Rate limit dostu:** parçalar arasında `--pause` sn beklenir. Bir parça başarısız olursa (çekicinin kendi
  üstel bekleme denemelerinden sonra) varsayılan olarak durulur: kaynak büyük olasılıkla hız sınırındadır ve
  yüklenmiş parçalar zaten kalıcıdır. `--keep-going` diğer parçalarla devam eder.

Boşluk onarımı (`--repair-gaps`): son `--days` günde `bvt.gaps`'in bulduğu gerçek eksik günler (`missing`;
tatiller değil) yakın günler birleştirilerek yeniden çekilir, ardından boşluk raporu yenilenir.

Kullanım:
    python -m bvt.backfill --start 2018-01-01                         # üç kaynak, bugüne kadar
    python -m bvt.backfill --source stocks --start 2015-01-01 --end 2019-12-31 --pause 5
    python -m bvt.backfill --source fx --start 2020-01-01 --dry-run   # yalnızca planı göster
    python -m bvt.backfill --repair-gaps --days 90
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from bvt import gaps
from bvt.dates import chunk_ranges
from bvt.pipeline import SOURCES, run_source

log = logging.getLogger("bvt.backfill")

CHUNK_DAYS = 365
PAUSE = 2.0

Window = tuple[date, date]
Runner = Callable[..., int]


@dataclass
class BackfillReport:
    source: str
    planned: int = 0
    skipped: int = 0
    loaded: list[Window] = field(default_factory=list)
    failed: list[Window] = field(default_factory=list)
    rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def __str__(self) -> str:
        return (f"{self.source}: {self.planned} parça, {self.skipped} atlandı (zaten yüklü), {len(self.loaded)} "
                f"yüklendi, {len(self.failed)} başarısız, {self.rows} satır")


def is_covered(window: Window, done: Iterable[Window]) -> bool:
    """`window` tamamen tek bir başarılı çalışmanın penceresi içinde mi?"""
    s, e = window
    return any(ds <= s and e <= de for ds, de in done)


def completed_windows(conn, source: str) -> list[Window]:
    """ops.pipeline_runs'tan kaynağın başarılı çalışma pencereleri."""
    rows = conn.execute(
        "SELECT window_start, window_end FROM ops.pipeline_runs WHERE source = %s AND status = 'success' "
        "AND window_start IS NOT NULL AND window_end IS NOT NULL",
        (source,),
    ).fetchall()
    return [(s, e) for s, e in rows]


def backfill_source(source: str, start: date, end: date, *, chunk_days: int = CHUNK_DAYS, pause: float = PAUSE,
                    done: Iterable[Window] = (), keep_going: bool = False, dry_run: bool = False,
                    runner: Runner = run_source, sleep: Callable[[float], None] = time.sleep) -> BackfillReport:
    """Bir kaynağı parça parça yükler. `done` içindeki pencerelerin kapsadığı parçalar atlanır."""
    if source not in SOURCES:
        raise ValueError(f"Bilinmeyen kaynak: {source!r}. Geçerli: {sorted(SOURCES)}")
    done = list(done)
    chunks = list(chunk_ranges(start, end, chunk_days))
    pending = [c for c in chunks if not is_covered(c, done)]
    report = BackfillReport(source, planned=len(chunks), skipped=len(chunks) - len(pending))
    log.info("%s: %s..%s, %d parça (%d zaten yüklü)", source, start, end, len(chunks), report.skipped)
    if dry_run:
        for s, e in pending:
            print(f"{source:<7} {s} .. {e}")
        return report

    for i, (s, e) in enumerate(pending):
        if i and pause:
            sleep(pause)
        try:
            report.rows += runner(source, s, e, trigger="backfill")
            report.loaded.append((s, e))
        except Exception as exc:  # noqa: BLE001 - parça hatası raporlanır, karar keep_going'e göre
            report.failed.append((s, e))
            if not keep_going:
                remaining = len(pending) - i - 1
                log.error("%s: %s..%s başarısız (%s); durduruldu, %d parça kaldı. Aynı komutla devam edilir.",
                          source, s, e, exc, remaining)
                break
            log.error("%s: %s..%s başarısız (%s); sonraki parçaya geçiliyor", source, s, e, exc)
    log.info("%s", report)
    return report


def repair_source(conn, source: str, start: date, end: date, *, max_gap_days: int = 7, pause: float = PAUSE,
                  runner: Runner = run_source, sleep: Callable[[float], None] = time.sleep) -> BackfillReport:
    """Kaynağın [start, end] içindeki gerçek eksik günlerini yeniden çeker, boşluk raporunu yeniler."""
    found = gaps.check_source(conn, source, start, end, save=False)
    windows = gaps.merge_windows((g.gap_date for g in found if g.kind == gaps.MISSING), max_gap_days)
    report = BackfillReport(source, planned=len(windows))
    for i, (s, e) in enumerate(windows):
        if i and pause:
            sleep(pause)
        try:
            report.rows += runner(source, s, e, trigger="repair")
            report.loaded.append((s, e))
        except Exception as exc:  # noqa: BLE001
            report.failed.append((s, e))
            log.error("%s: %s..%s onarılamadı: %s", source, s, e, exc)
    after = gaps.check_source(conn, source, start, end, save=True)
    still = sum(1 for g in after if g.kind == gaps.MISSING)
    if still:
        # Kaynakta da yoksa (ör. sembolde işlem durdurulmuş) yeniden çekim düzeltemez; rapor kalır.
        log.warning("%s: onarımdan sonra %d eksik gün kaldı (kaynakta da olmayabilir)", source, still)
    log.info("%s (onarım)", report)
    return report


# --------------------------------------------------------------------------- CLI
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Geçmiş yükleme (parça parça, devam ettirilebilir) ve boşluk onarımı.")
    p.add_argument("--source", nargs="+", choices=sorted(SOURCES), default=list(SOURCES))
    p.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD (backfill için zorunlu)")
    p.add_argument("--end", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (dahil)")
    p.add_argument("--chunk-days", type=int, default=CHUNK_DAYS, help="Parça (istek) başına gün sayısı")
    p.add_argument("--pause", type=float, default=PAUSE, help="Parçalar arası bekleme (sn)")
    p.add_argument("--no-resume", action="store_true", help="Daha önce yüklenen parçaları da yeniden çek")
    p.add_argument("--keep-going", action="store_true", help="Başarısız parçadan sonra devam et")
    p.add_argument("--dry-run", action="store_true", help="Yalnızca çekilecek parçaları listele")
    p.add_argument("--repair-gaps", action="store_true", help="Son --days gündeki gerçek eksikleri yeniden çek")
    p.add_argument("--days", type=int, default=gaps.DEFAULT_CHECK_DAYS, help="--repair-gaps için geriye gün")
    args = p.parse_args(argv)
    if not args.repair_gaps and args.start is None:
        p.error("--start gerekli (ya da --repair-gaps kullanın)")
    if args.start and args.start > args.end:
        p.error(f"--start ({args.start}) --end'den ({args.end}) sonra olamaz")
    if args.chunk_days < 1 or args.pause < 0 or args.days < 0:
        p.error("--chunk-days >= 1, --pause >= 0 ve --days >= 0 olmalı")
    return args


def main(argv: list[str] | None = None) -> int:
    from bvt.db import connect
    from bvt.logging_setup import setup_logging

    setup_logging()
    args = parse_args(argv)
    reports: list[BackfillReport] = []
    try:
        with connect() as conn:
            for i, source in enumerate(args.source):
                if i and args.pause:
                    time.sleep(args.pause)
                if args.repair_gaps:
                    reports.append(repair_source(conn, source, args.end - timedelta(days=args.days), args.end,
                                                 pause=args.pause))
                    continue
                done = [] if args.no_resume else completed_windows(conn, source)
                reports.append(backfill_source(source, args.start, args.end, chunk_days=args.chunk_days,
                                               pause=args.pause, done=done, keep_going=args.keep_going,
                                               dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001
        log.error("Backfill başarısız: %s", exc)
        return 1
    for r in reports:
        print(r)
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
