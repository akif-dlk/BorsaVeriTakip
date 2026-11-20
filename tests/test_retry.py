import pytest

from bvt import retry as retry_mod
from bvt.retry import TRANSIENT_ERRORS, backoff_delay, is_rate_limited, retry, retry_call


class RateLimited(Exception):
    pass


RateLimited.__name__ = "YFRateLimitError"


def test_succeeds_after_transient_failures():
    sleeps, calls = [], []

    @retry(attempts=3, base_delay=1.0, exceptions=(ConnectionError,), sleep=sleeps.append)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3 and len(sleeps) == 2
    assert 1.0 <= sleeps[0] < 1.6 and 2.0 <= sleeps[1] < 2.6  # 1, 2 sn + jitter


def test_raises_after_last_attempt():
    sleeps = []

    @retry(attempts=2, base_delay=0.1, exceptions=(ConnectionError,), sleep=sleeps.append)
    def always():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        always()
    assert len(sleeps) == 1


def test_non_transient_error_is_not_retried():
    calls = []

    @retry(attempts=4, exceptions=TRANSIENT_ERRORS, sleep=lambda s: pytest.fail("beklememeli"))
    def bug():
        calls.append(1)
        raise ValueError("programlama hatası")

    with pytest.raises(ValueError):
        bug()
    assert calls == [1]


def test_rate_limit_waits_longer():
    sleeps = []

    @retry(attempts=2, base_delay=1.0, exceptions=(RateLimited,), sleep=sleeps.append, rate_limit_delay=30.0)
    def limited():
        raise RateLimited("Too Many Requests. Rate limited.")

    with pytest.raises(RateLimited):
        limited()
    assert sleeps[0] >= 30.0


@pytest.mark.parametrize("exc, expected", [
    (RateLimited("x"), True),
    (RuntimeError("EVDS HTTP 429"), True),
    (RuntimeError("429 Client Error: Too Many Requests"), True),
    (RuntimeError("close 429.5 geçersiz"), False),
    (ConnectionError("reset"), False),
])
def test_is_rate_limited(exc, expected):
    assert is_rate_limited(exc) is expected


def test_is_rate_limited_reads_response_status():
    exc = RuntimeError("hata")
    exc.response = type("R", (), {"status_code": 429})()
    assert is_rate_limited(exc)


def test_backoff_is_capped():
    assert backoff_delay(10, base_delay=2.0, max_delay=60.0, jitter=0) == 60.0
    assert backoff_delay(1, base_delay=2.0, max_delay=60.0, jitter=0) == 2.0


def test_transient_errors_include_network_and_yfinance_rate_limit():
    import requests
    from yfinance.exceptions import YFRateLimitError

    assert {ConnectionError, TimeoutError, requests.RequestException, YFRateLimitError} <= set(TRANSIENT_ERRORS)


def test_retry_call(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)
    state = {"n": 0}

    def f(x, y=0):
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError
        return x + y

    assert retry_call(f, 1, y=2, attempts=2, exceptions=(TimeoutError,)) == 3


def test_attempts_must_be_positive():
    with pytest.raises(ValueError):
        retry(attempts=0)
