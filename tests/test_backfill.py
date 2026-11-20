from datetime import date

import pytest

from bvt import backfill, gaps
from bvt.gaps import MISSING, Gap


class Runner:
    def __init__(self, fail_on=()):
        self.calls, self.fail_on = [], set(fail_on)

    def __call__(self, source, start, end, trigger):
        self.calls.append((source, start, end, trigger))
        if start in self.fail_on:
            raise RuntimeError("rate limited")
        return 10


def test_chunks_and_trigger():
    run, sleeps = Runner(), []
    r = backfill.backfill_source("stocks", date(2020, 1, 1), date(2022, 6, 30), chunk_days=365, pause=2,
                                 runner=run, sleep=sleeps.append)
    assert [(c[1], c[2]) for c in run.calls] == [(date(2020, 1, 1), date(2020, 12, 30)),
                                                 (date(2020, 12, 31), date(2021, 12, 30)),
                                                 (date(2021, 12, 31), date(2022, 6, 30))]
    assert {c[3] for c in run.calls} == {"backfill"}
    assert sleeps == [2, 2]  # parçalar arası bekleme, ilkinden önce yok
    assert r.ok and r.rows == 30 and r.planned == 3 and r.skipped == 0


def test_resume_skips_covered_chunks():
    run = Runner()
    done = [(date(2020, 1, 1), date(2020, 12, 30)), (date(2021, 1, 5), date(2021, 1, 10))]
    r = backfill.backfill_source("fx", date(2020, 1, 1), date(2021, 12, 30), chunk_days=365, pause=0,
                                 done=done, runner=run)
    assert [c[1] for c in run.calls] == [date(2020, 12, 31)]  # kısmen kapsanan parça yeniden çekilir
    assert r.skipped == 1


def test_stops_on_first_failure_by_default():
    run = Runner(fail_on={date(2021, 1, 1)})
    r = backfill.backfill_source("gold", date(2020, 1, 1), date(2022, 12, 31), chunk_days=366, pause=0,
                                 runner=run)
    assert len(run.calls) == 2 and r.failed == [(date(2021, 1, 1), date(2022, 1, 1))] and not r.ok


def test_keep_going_continues_after_failure():
    run = Runner(fail_on={date(2021, 1, 1)})
    r = backfill.backfill_source("gold", date(2020, 1, 1), date(2022, 12, 31), chunk_days=366, pause=0,
                                 runner=run, keep_going=True)
    assert len(run.calls) == 3 and len(r.loaded) == 2 and len(r.failed) == 1


def test_dry_run_does_not_fetch(capsys):
    run = Runner()
    r = backfill.backfill_source("fx", date(2024, 1, 1), date(2024, 3, 1), chunk_days=31, runner=run, dry_run=True)
    assert run.calls == [] and r.planned == 2
    assert "2024-01-01 .. 2024-01-31" in capsys.readouterr().out


def test_unknown_source():
    with pytest.raises(ValueError):
        backfill.backfill_source("crypto", date(2024, 1, 1), date(2024, 1, 2), runner=Runner())


def test_is_covered():
    done = [(date(2024, 1, 1), date(2024, 1, 31))]
    assert backfill.is_covered((date(2024, 1, 5), date(2024, 1, 31)), done)
    assert not backfill.is_covered((date(2024, 1, 5), date(2024, 2, 1)), done)
    assert not backfill.is_covered((date(2024, 1, 5), date(2024, 1, 6)), [])


def test_completed_windows_query():
    class Conn:
        def execute(self, sql, params):
            assert "status = 'success'" in sql and params == ("fx",)
            return type("R", (), {"fetchall": lambda s: [(date(2024, 1, 1), date(2024, 1, 7))]})()

    assert backfill.completed_windows(Conn(), "fx") == [(date(2024, 1, 1), date(2024, 1, 7))]


def test_repair_refetches_only_missing_days(monkeypatch):
    d = date(2026, 11, 2)
    before = [Gap("stocks", d, "*", gaps.MARKET_CLOSED), Gap("stocks", date(2026, 11, 4), "THYAO", MISSING),
              Gap("stocks", date(2026, 11, 6), "GARAN", MISSING), Gap("stocks", date(2026, 11, 25), "THYAO", MISSING)]
    checks = []

    def fake_check(conn, source, start, end, save=True):
        checks.append(save)
        return before if len(checks) == 1 else []

    monkeypatch.setattr(gaps, "check_source", fake_check)
    run = Runner()
    r = backfill.repair_source(object(), "stocks", date(2026, 10, 25), date(2026, 11, 30), pause=0, runner=run)
    assert [(c[1], c[2], c[3]) for c in run.calls] == [(date(2026, 11, 4), date(2026, 11, 6), "repair"),
                                                       (date(2026, 11, 25), date(2026, 11, 25), "repair")]
    assert checks == [False, True]  # önce raporla, onarımdan sonra anlık görüntüyü yaz
    assert r.ok and r.planned == 2


def test_parse_args_requires_start_unless_repair():
    with pytest.raises(SystemExit):
        backfill.parse_args([])
    assert backfill.parse_args(["--repair-gaps"]).repair_gaps
    args = backfill.parse_args(["--start", "2020-01-01", "--end", "2020-12-31", "--source", "fx"])
    assert args.source == ["fx"] and args.chunk_days == backfill.CHUNK_DAYS
    with pytest.raises(SystemExit):
        backfill.parse_args(["--start", "2021-01-01", "--end", "2020-12-31"])
