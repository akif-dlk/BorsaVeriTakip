import sys
import types
from datetime import date, datetime, timezone

import pytest

from bvt import pipeline


def test_heavy_modules_not_imported_at_module_level():
    # DAG ayrıştırması bvt.pipeline'ı içe aktarır; pandas/yfinance yüklememeli.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pipeline))
    top_imports = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top_imports |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert not top_imports & {"pandas", "yfinance", "psycopg", "bvt"}


def test_market_date_converts_utc_to_istanbul():
    # 21:30 UTC = ertesi gün 00:30 İstanbul
    assert pipeline.market_date(datetime(2026, 11, 17, 21, 30, tzinfo=timezone.utc)) == date(2026, 11, 18)
    assert pipeline.market_date(datetime(2026, 11, 17, 16, 30, tzinfo=timezone.utc)) == date(2026, 11, 17)


def test_market_date_naive_is_utc_and_date_passthrough():
    assert pipeline.market_date(datetime(2026, 11, 17, 22, 0)) == date(2026, 11, 18)
    assert pipeline.market_date(date(2026, 11, 17)) == date(2026, 11, 17)


def test_daily_window_default_and_explicit(monkeypatch):
    monkeypatch.delenv("BVT_LOOKBACK_DAYS", raising=False)
    assert pipeline.daily_window(date(2026, 11, 18)) == (date(2026, 11, 11), date(2026, 11, 18))
    assert pipeline.daily_window(date(2026, 11, 18), 0) == (date(2026, 11, 18), date(2026, 11, 18))


def test_lookback_from_env(monkeypatch):
    monkeypatch.setenv("BVT_LOOKBACK_DAYS", "3")
    assert pipeline.daily_window(date(2026, 11, 18)) == (date(2026, 11, 15), date(2026, 11, 18))
    monkeypatch.setenv("BVT_LOOKBACK_DAYS", "")  # boş -> varsayılan
    assert pipeline.lookback_days() == pipeline.DEFAULT_LOOKBACK_DAYS


@pytest.mark.parametrize("bad", ["abc", "-1"])
def test_lookback_invalid(monkeypatch, bad):
    monkeypatch.setenv("BVT_LOOKBACK_DAYS", bad)
    with pytest.raises(ValueError):
        pipeline.lookback_days()


@pytest.fixture
def fake_fetchers(monkeypatch):
    calls = []
    for name, module in pipeline.SOURCES.items():
        fake = types.ModuleType(module)
        fake.run = lambda start, end, _n=name: calls.append((_n, start, end)) or 42
        monkeypatch.setitem(sys.modules, module, fake)
    return calls


def test_run_source_dispatches_to_fetcher(fake_fetchers):
    assert pipeline.run_source("gold", date(2026, 11, 1), date(2026, 11, 5)) == 42
    assert fake_fetchers == [("gold", date(2026, 11, 1), date(2026, 11, 5))]


def test_run_source_rejects_unknown_and_inverted(fake_fetchers):
    with pytest.raises(ValueError, match="Bilinmeyen"):
        pipeline.run_source("crypto", date(2026, 11, 1), date(2026, 11, 5))
    with pytest.raises(ValueError):
        pipeline.run_source("fx", date(2026, 11, 6), date(2026, 11, 5))
    assert fake_fetchers == []


def test_run_daily_returns_xcom_summary(fake_fetchers):
    out = pipeline.run_daily("stocks", datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc), lookback=2)
    assert out == {"source": "stocks", "start": "2026-11-16", "end": "2026-11-18", "rows": 42}


def test_run_daily_propagates_fetcher_error(monkeypatch):
    fake = types.ModuleType("bvt.fetch_fx")

    def boom(start, end):
        raise RuntimeError("yfinance down")

    fake.run = boom
    monkeypatch.setitem(sys.modules, "bvt.fetch_fx", fake)
    with pytest.raises(RuntimeError, match="yfinance down"):
        pipeline.run_daily("fx", date(2026, 11, 18))


def test_main_continues_after_failure(monkeypatch, fake_fetchers):
    def boom(start, end):
        raise RuntimeError("x")

    sys.modules["bvt.fetch_fx"].run = boom
    rc = pipeline.main(["--end", "2026-11-18", "--lookback-days", "1"])
    assert rc == 1
    assert [c[0] for c in fake_fetchers] == ["stocks", "gold"]


def test_main_selected_sources(fake_fetchers):
    assert pipeline.main(["--source", "gold", "--end", "2026-11-18", "--lookback-days", "0"]) == 0
    assert fake_fetchers == [("gold", date(2026, 11, 18), date(2026, 11, 18))]


def test_run_source_sets_trigger_for_fetcher(monkeypatch):
    from bvt.run_context import current_trigger

    seen = []
    fake = types.ModuleType("bvt.fetch_fx")
    fake.run = lambda start, end: seen.append(current_trigger()) or 3
    monkeypatch.setitem(sys.modules, "bvt.fetch_fx", fake)
    assert pipeline.run_source("fx", date(2026, 11, 1), date(2026, 11, 2), trigger="backfill") == 3
    assert seen == ["backfill"] and current_trigger() == "manual"


def test_run_source_logs_failure_with_duration(monkeypatch, caplog):
    fake = types.ModuleType("bvt.fetch_gold")

    def boom(start, end):
        raise RuntimeError("yahoo 500")

    fake.run = boom
    monkeypatch.setitem(sys.modules, "bvt.fetch_gold", fake)
    with pytest.raises(RuntimeError):
        pipeline.run_source("gold", date(2026, 11, 1), date(2026, 11, 2))
    rec = next(r for r in caplog.records if r.levelname == "ERROR")
    assert rec.source == "gold" and hasattr(rec, "duration_s")


def test_check_gaps_summary(monkeypatch):
    from bvt import gaps
    from bvt.gaps import MISSING, Gap

    calls = {}

    def fake_check(sources, start, end):
        calls.update(sources=sources, start=start, end=end)
        return {s: [Gap(s, end, "X", MISSING)] if s == "stocks" else [] for s in sources}

    monkeypatch.setattr(gaps, "check", fake_check)
    monkeypatch.delenv("BVT_GAP_CHECK_DAYS", raising=False)
    out = pipeline.check_gaps(datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc))
    assert calls == {"sources": list(pipeline.SOURCES), "start": date(2026, 10, 19), "end": date(2026, 11, 18)}
    assert out["stocks"]["missing"] == 1 and out["fx"]["missing"] == 0 and out["end"] == "2026-11-18"
    monkeypatch.setenv("BVT_GAP_CHECK_DAYS", "5")
    assert pipeline.check_gaps(date(2026, 11, 18))["start"] == "2026-11-13"
