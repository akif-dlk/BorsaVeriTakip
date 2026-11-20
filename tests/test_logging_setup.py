import io
import json
import logging

import pytest

from bvt.logging_setup import setup_logging
from bvt.run_context import TRIGGERS, current_trigger, run_trigger


@pytest.fixture(autouse=True)
def restore_root():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_json_format_includes_extra_trigger_and_exception():
    buf = io.StringIO()
    setup_logging("INFO", "json", stream=buf)
    log = logging.getLogger("bvt.test")
    with run_trigger("backfill"):
        log.info("yüklendi %d", 5, extra={"source": "fx", "rows": 5})
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("hata")
    first, second = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert first["msg"] == "yüklendi 5" and first["level"] == "INFO" and first["logger"] == "bvt.test"
    assert first["source"] == "fx" and first["rows"] == 5 and first["trigger"] == "backfill"
    assert first["ts"].endswith("+00:00")
    assert second["trigger"] == "manual" and "RuntimeError: boom" in second["exc"]


def test_text_format_and_level_from_env(monkeypatch):
    monkeypatch.setenv("BVT_LOG_LEVEL", "warning")
    monkeypatch.delenv("BVT_LOG_FORMAT", raising=False)
    buf = io.StringIO()
    setup_logging(stream=buf)
    logging.getLogger("bvt.test").info("görünmemeli")
    logging.getLogger("bvt.test").warning("uyarı")
    out = buf.getvalue()
    assert "görünmemeli" not in out and "WARNING bvt.test: uyarı" in out


def test_repeated_setup_does_not_duplicate_handlers():
    setup_logging("INFO", "text", stream=io.StringIO())
    setup_logging("INFO", "text", stream=io.StringIO())
    assert sum(getattr(h, "_bvt", False) for h in logging.getLogger().handlers) == 1


def test_noisy_libraries_quieted():
    setup_logging("DEBUG", "text", stream=io.StringIO())
    assert logging.getLogger("yfinance").level == logging.WARNING


@pytest.mark.parametrize("kwargs", [{"fmt": "xml"}, {"level": "LOUD"}])
def test_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        setup_logging(**kwargs, stream=io.StringIO())


def test_run_trigger_context_restores_and_validates():
    assert current_trigger() == "manual"
    with run_trigger("airflow"):
        with run_trigger("repair"):
            assert current_trigger() == "repair"
        assert current_trigger() == "airflow"
    assert current_trigger() == "manual"
    assert "backfill" in TRIGGERS
    with pytest.raises(ValueError):
        with run_trigger("cron"):
            pass
