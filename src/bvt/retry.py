"""Üstel bekleme (exponential backoff) ile yeniden deneme yardımcısı."""
from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def retry(
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Fonksiyonu en fazla `attempts` kez dener; denemeler arasında 2, 4, 8... sn (+ küçük jitter) bekler.

    Son denemede de hata alınırsa özgün hata yukarı fırlatılır.
    """
    if attempts < 1:
        raise ValueError("attempts >= 1 olmalı")

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == attempts:
                        log.error("%s %d denemede başarısız: %s", func.__name__, attempts, exc)
                        raise
                    delay = min(max_delay, base_delay * 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    log.warning("%s hata (%d/%d): %s; %.1f sn sonra tekrar", func.__name__, attempt, attempts,
                                exc, delay)
                    (sleep or time.sleep)(delay)
            raise AssertionError("ulaşılamaz")  # pragma: no cover

        return wrapper

    return decorator
