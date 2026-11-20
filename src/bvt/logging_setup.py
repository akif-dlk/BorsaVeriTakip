"""Ortak loglama ayarı: tüm CLI giriş noktaları (`python -m bvt.*`) aynı biçimde log yazar.

- `BVT_LOG_LEVEL` (varsayılan INFO): kök log seviyesi.
- `BVT_LOG_FORMAT` (`text` | `json`, varsayılan text): `json` her satırı tek bir JSON nesnesi olarak yazar
  (zaman damgası UTC, seviye, logger, mesaj, tetikleyici, varsa `extra` alanları ve hata yığını); log
  toplayıcılara (Loki, ELK) doğrudan beslenebilir.
- yfinance/urllib3 gibi gürültülü kütüphaneler WARNING seviyesine çekilir.

Airflow görevlerinde çağrılmaz: Airflow kendi görev loglayıcısını kurar, bizim loglarımız oraya akar.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from bvt.run_context import current_trigger

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
NOISY_LOGGERS = ("yfinance", "urllib3", "peewee", "requests")
# LogRecord'un standart alanları; bunların dışındakiler `extra=` ile eklenmiştir ve JSON'a yazılır.
_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trigger": current_trigger(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS and not k.startswith("_")})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, fmt: str | None = None, stream=None) -> logging.Handler:
    """Kök loglayıcıya tek bir bvt handler'ı kurar. Tekrar çağrılırsa öncekinin yerine geçer (kopya log olmaz)."""
    level = (level or os.getenv("BVT_LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.getenv("BVT_LOG_FORMAT") or "text").lower()
    if fmt not in ("text", "json"):
        raise ValueError(f"BVT_LOG_FORMAT 'text' ya da 'json' olmalı: {fmt!r}")
    if not isinstance(logging.getLevelName(level), int):
        raise ValueError(f"Geçersiz log seviyesi: {level!r}")

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    handler._bvt = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_bvt", False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
    return handler
