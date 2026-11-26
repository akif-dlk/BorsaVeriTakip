"""bvt.dbt_runner: komut kurulumu, çıktı özeti ve hata davranışı (dbt'nin kendisi çalıştırılmaz)."""
import subprocess
import sys

import pytest

from bvt import dbt_runner

DONE_LINE = "07:05:12  Done. PASS=58 WARN=1 ERROR=0 SKIP=0 NO-OP=0 TOTAL=59"


@pytest.fixture(autouse=True)
def no_result_recording(monkeypatch, request):
    """build_* sonrası test sonucu kaydı veritabanına gider; ayrıca test edilenler dışında devre dışı."""
    if "record" not in request.node.name:
        monkeypatch.setattr(dbt_runner, "record_test_results", lambda started_at: 0)


def test_executable_prefers_env(monkeypatch):
    monkeypatch.setenv("DBT_BIN", "/opt/dbt-venv/bin/dbt")
    assert dbt_runner.dbt_executable() == "/opt/dbt-venv/bin/dbt"


def test_executable_missing_gives_install_hint(monkeypatch):
    monkeypatch.delenv("DBT_BIN", raising=False)
    monkeypatch.setattr(dbt_runner.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="requirements-dbt.txt"):
        dbt_runner.dbt_executable()


def test_build_command_adds_project_and_profiles_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DBT_BIN", "dbt")
    monkeypatch.setenv("BVT_DBT_DIR", str(tmp_path))
    cmd = dbt_runner.build_command(["build", "--select", "staging"])
    assert cmd == ["dbt", "build", "--select", "staging", "--project-dir", str(tmp_path),
                   "--profiles-dir", str(tmp_path)]


def test_build_command_keeps_user_dirs(monkeypatch):
    monkeypatch.setenv("DBT_BIN", "dbt")
    cmd = dbt_runner.build_command(["debug", "--profiles-dir", "/x"])
    assert cmd.count("--profiles-dir") == 1 and "--project-dir" in cmd


def test_default_dir_is_repo_dbt_project(monkeypatch):
    monkeypatch.delenv("BVT_DBT_DIR", raising=False)
    assert (dbt_runner.dbt_dir() / "dbt_project.yml").is_file()


def test_env_forces_utf8(monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    assert dbt_runner.dbt_env()["PYTHONUTF8"] == "1"


def test_summarize_parses_done_line():
    lines = ["Running with dbt=1.9.4", "1 of 59 OK created sql view model staging.stg_fx_rates", DONE_LINE]
    assert dbt_runner.summarize(lines) == {"pass": 58, "warn": 1, "error": 0, "skip": 0, "no-op": 0, "total": 59}
    assert dbt_runner.summarize(["no summary"]) == {}


@pytest.fixture
def fake_dbt(monkeypatch, tmp_path):
    """dbt yerine küçük bir Python betiği çalıştırır: gerçek alt süreç yolu (çıktı okuma, çıkış kodu) test edilir."""
    monkeypatch.setenv("BVT_DBT_DIR", str(tmp_path))

    def make(output: str, code: int = 0) -> None:
        script = tmp_path / "fake_dbt.py"
        script.write_text(
            f"import sys\nprint({output!r})\nprint(' '.join(sys.argv[1:]))\nsys.exit({code})\n", encoding="utf-8"
        )
        monkeypatch.setenv("DBT_BIN", sys.executable)
        real = dbt_runner.build_command
        monkeypatch.setattr(dbt_runner, "build_command", lambda args: [sys.executable, str(script), *real(args)[1:]])

    return make


def test_run_dbt_success_returns_lines(fake_dbt):
    fake_dbt(DONE_LINE)
    lines = dbt_runner.run_dbt(["build", "--select", "staging"])
    assert lines[0] == DONE_LINE
    assert "build --select staging --project-dir" in lines[1]


def test_run_dbt_failure_raises_with_tail(fake_dbt):
    fake_dbt("Failure in test not_null_stg_fx_rates_close", code=1)
    with pytest.raises(dbt_runner.DbtError) as info:
        dbt_runner.run_dbt(["build"])
    assert info.value.returncode == 1
    assert "not_null_stg_fx_rates_close" in str(info.value)


def test_timeout_kills_process(monkeypatch, tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("BVT_DBT_DIR", str(tmp_path))
    monkeypatch.setattr(dbt_runner, "build_command", lambda args: [sys.executable, str(script)])
    with pytest.raises(subprocess.TimeoutExpired):
        dbt_runner.run_dbt(["build"], timeout=0.5)


def test_build_staging_selects_sources_and_staging(monkeypatch):
    seen = {}

    def fake_run(args, timeout=None):
        seen["args"] = args
        return [DONE_LINE]

    monkeypatch.setattr(dbt_runner, "run_dbt", fake_run)
    result = dbt_runner.build_staging()
    assert seen["args"] == ["build", "--select", "source:raw", "staging"]
    assert result["select"] == "source:raw staging" and result["pass"] == 58


def test_main_returns_1_on_failure(monkeypatch):
    def boom(args, timeout=None):
        raise dbt_runner.DbtError(args, 2, ["Compilation Error"])

    monkeypatch.setattr(dbt_runner, "run_dbt", boom)
    monkeypatch.setattr("bvt.logging_setup.setup_logging", lambda *a, **k: None)
    assert dbt_runner.main(["build"]) == 1


def test_build_marts_selects_seed_and_marts(monkeypatch):
    seen = {}

    def fake_run(args, timeout=None):
        seen.update(args=args, timeout=timeout)
        return [DONE_LINE]

    monkeypatch.setattr(dbt_runner, "run_dbt", fake_run)
    result = dbt_runner.build_marts()
    assert seen["args"] == ["build", "--select", "instrument_metadata", "marts"]
    assert seen["timeout"] == dbt_runner.BUILD_TIMEOUT_S
    assert result["select"] == "instrument_metadata marts" and result["total"] == 59


def test_build_records_test_results_even_when_dbt_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(dbt_runner, "record_test_results", lambda started_at: calls.append(started_at) or 0)

    def boom(args, timeout=None):
        raise dbt_runner.DbtError(args, 1, ["FAIL 1 assert_gram_gold_matches_ounce"])

    monkeypatch.setattr(dbt_runner, "run_dbt", boom)
    with pytest.raises(dbt_runner.DbtError):
        dbt_runner.build_marts()
    assert len(calls) == 1 and calls[0].tzinfo is not None


def test_record_test_results_never_raises(monkeypatch):
    import bvt.dbt_report

    def broken(started_after=None):
        raise RuntimeError("veritabanı yok")

    monkeypatch.setattr(bvt.dbt_report, "record_latest", broken)
    from datetime import datetime, timezone

    assert dbt_runner.record_test_results(datetime.now(timezone.utc)) == 0
