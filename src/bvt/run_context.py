"""Çalışmanın kim tarafından başlatıldığını (tetikleyici) taşıyan hafif bağlam.

`ops.pipeline_runs.run_trigger` sütunu ve JSON loglar bu değeri kullanır; böylece sağlık panelinde günlük
Airflow çalışmaları, geçmiş yüklemeler (backfill) ve boşluk onarımları birbirinden ayrılır. Yalnızca standart
kütüphane kullanır (bvt.pipeline gibi DAG ayrıştırmasında yüklenen modüllerden güvenle içe aktarılabilir).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

TRIGGERS = ("manual", "airflow", "backfill", "repair")
_TRIGGER: ContextVar[str] = ContextVar("bvt_run_trigger", default="manual")


def current_trigger() -> str:
    return _TRIGGER.get()


@contextmanager
def run_trigger(name: str) -> Iterator[str]:
    """Blok süresince tetikleyiciyi `name` yapar; çıkışta önceki değere döner."""
    if name not in TRIGGERS:
        raise ValueError(f"Bilinmeyen tetikleyici: {name!r}. Geçerli: {TRIGGERS}")
    token = _TRIGGER.set(name)
    try:
        yield name
    finally:
        _TRIGGER.reset(token)
