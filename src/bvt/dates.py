"""Tarih aralığı yardımcıları (yalnızca standart kütüphane)."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta


def chunk_ranges(start: date, end: date, chunk_days: int) -> Iterator[tuple[date, date]]:
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
