"""Üstel bekleme (exponential backoff) ile yeniden deneme yardımcıları.

- `retry(...)`: dekoratör. Denemeler arasında base, 2×base, 4×base... sn (+ küçük jitter, üst sınır max_delay).
- `retry_call(func, ...)`: aynı mantığın tek seferlik çağrı biçimi (ör. DB bağlantısı).
- Hız sınırı (HTTP 429 / yfinance `YFRateLimitError`) hatalarında bekleme en az `rate_limit_delay` sn olur:
  kısa aralıklarla yeniden denemek engeli uzatır.
- `TRANSIENT_ERRORS`: geçici sayılan ağ hataları. Programlama hataları (ValueError, KeyError...) yeniden
  denenmez, hemen yukarı fırlatılır.
"""
from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def _transient_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [ConnectionError, TimeoutError]
    try:
        import requests

        errors.append(requests.RequestException)
    except ImportError:  # pragma: no cover
        pass
    try:
        from yfinance.exceptions import YFRateLimitError

        errors.append(YFRateLimitError)
    except ImportError:  # pragma: no cover - eski/yeni yfinance sürümleri
        pass
    return tuple(errors)


TRANSIENT_ERRORS: tuple[type[BaseException], ...] = _transient_errors()


def is_rate_limited(exc: BaseException) -> bool:
    """Hata kaynağın hız sınırından mı geliyor? (HTTP 429 ya da adında RateLimit geçen istisna)"""
    if "ratelimit" in type(exc).__name__.lower():
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    msg = str(exc)[:300].lower()
    return "http 429" in msg or "too many requests" in msg


def backoff_delay(attempt: int, base_delay: float, max_delay: float, exc: BaseException | None = None,
                  rate_limit_delay: float = 0.0, jitter: float = 0.5) -> float:
    """`attempt`. başarısız denemeden sonra beklenecek süre (sn). attempt 1'den başlar."""
    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
    if exc is not None and is_rate_limited(exc):
        delay = max(delay, rate_limit_delay)
    return delay + random.uniform(0, jitter)


def retry(
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] | None = None,
    rate_limit_delay: float = 30.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Fonksiyonu en fazla `attempts` kez dener; yalnızca `exceptions` türündeki hatalarda yeniden dener.

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
                        log.error("%s %d denemede başarısız: %s", func.__name__, attempts, exc,
                                  extra={"func": func.__name__, "attempt": attempt, "attempts": attempts})
                        raise
                    delay = backoff_delay(attempt, base_delay, max_delay, exc, rate_limit_delay)
                    log.warning("%s hata (%d/%d): %s: %s; %.1f sn sonra tekrar", func.__name__, attempt, attempts,
                                type(exc).__name__, exc, delay,
                                extra={"func": func.__name__, "attempt": attempt, "attempts": attempts,
                                       "delay_s": round(delay, 1), "rate_limited": is_rate_limited(exc)})
                    (sleep or time.sleep)(delay)
            raise AssertionError("ulaşılamaz")  # pragma: no cover

        return wrapper

    return decorator


def retry_call(func: Callable[..., T], *args, attempts: int = 4, base_delay: float = 2.0, max_delay: float = 60.0,
               exceptions: tuple[type[BaseException], ...] = (Exception,),
               sleep: Callable[[float], None] | None = None, **kwargs) -> T:
    """`retry` dekoratörünün tek çağrılık hali: `retry_call(psycopg.connect, dsn, exceptions=(...))`."""
    return retry(attempts, base_delay, max_delay, exceptions, sleep)(func)(*args, **kwargs)
