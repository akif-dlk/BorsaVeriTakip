"""bvt.dbt_report: run_results.json ayrıştırma, kayıt, Markdown/JSON rapor (dbt ve PostgreSQL gerekmez)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from bvt import dbt_report as rep

GENERATED = "2026-11-26T16:45:12.123456Z"


def write_target(tmp_path, results, nodes=None, generated_at=GENERATED):
    (tmp_path / "run_results.json").write_text(json.dumps({
        "metadata": {"generated_at": generated_at, "invocation_id": "inv-1"},
        "results": results,
    }), encoding="utf-8")
    if nodes is not None:
        (tmp_path / "manifest.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return tmp_path


RESULTS = [
    {"unique_id": "model.bvt.fact_daily_prices", "status": "success", "failures": None, "message": "SELECT 10",
     "execution_time": 0.5},
    {"unique_id": "test.bvt.not_null_fact_daily_prices_close.abc", "status": "pass", "failures": 0,
     "message": None, "execution_time": 0.01},
    {"unique_id": "test.bvt.assert_gram_gold_matches_ounce", "status": "fail", "failures": 3,
     "message": "Got 3 results, configured to fail if != 0", "execution_time": 0.2},
    {"unique_id": "test.bvt.warn_recent_price_anomalies", "status": "warn", "failures": 1,
     "message": "Got 1 result, configured to warn if != 0", "execution_time": 0.02},
    {"unique_id": "test.bvt.source_not_null_raw_fx_rates_close.def", "status": "Runtime Error", "failures": None,
     "message": "relation \"raw.fx_rates\"\n does not exist", "execution_time": 0.0},
]
NODES = {
    "test.bvt.not_null_fact_daily_prices_close.abc": {
        "name": "not_null_fact_daily_prices_close", "test_metadata": {"name": "not_null"}, "column_name": "close",
        "attached_node": "model.bvt.fact_daily_prices", "config": {"severity": "ERROR"}},
    "test.bvt.assert_gram_gold_matches_ounce": {
        "name": "assert_gram_gold_matches_ounce", "column_name": None, "attached_node": None,
        "depends_on": {"nodes": ["model.bvt.stg_gold_prices"]}, "config": {"severity": "ERROR"}},
    "test.bvt.warn_recent_price_anomalies": {
        "name": "warn_recent_price_anomalies", "depends_on": {"nodes": ["model.bvt.mart_price_anomalies"]},
        "config": {"severity": "warn"}},
    "test.bvt.source_not_null_raw_fx_rates_close.def": {
        "name": "source_not_null_raw_fx_rates_close", "test_metadata": {"name": "not_null"}, "column_name": "close",
        "attached_node": None, "depends_on": {"nodes": ["source.bvt.raw.fx_rates"]}, "config": {"severity": "error"}},
}


def test_load_run_results_keeps_only_tests_and_enriches_from_manifest(tmp_path):
    run = rep.load_run_results(write_target(tmp_path, RESULTS, NODES))
    assert run.invocation_id == "inv-1"
    assert run.generated_at == datetime(2026, 11, 26, 16, 45, 12, 123456, tzinfo=timezone.utc)
    by_name = {t.test_name: t for t in run.tests}
    assert len(run.tests) == 4  # model sonucu atlandı
    t = by_name["not_null_fact_daily_prices_close"]
    assert (t.test_type, t.model, t.column_name, t.severity, t.status, t.failures) == \
           ("not_null", "fact_daily_prices", "close", "error", "pass", 0)
    singular = by_name["assert_gram_gold_matches_ounce"]
    assert (singular.test_type, singular.model, singular.status, singular.failures) == \
           ("singular", "stg_gold_prices", "fail", 3)
    assert by_name["warn_recent_price_anomalies"].severity == "warn"
    source = by_name["source_not_null_raw_fx_rates_close"]
    assert source.model == "raw.fx_rates"
    assert source.status == "error"  # "Runtime Error" → error
    assert source.message == 'relation "raw.fx_rates" does not exist'  # satır sonları tek boşluk


def test_load_run_results_without_manifest_falls_back_to_unique_id(tmp_path):
    run = rep.load_run_results(write_target(tmp_path, RESULTS[1:2]))
    t = run.tests[0]
    assert (t.test_name, t.test_type, t.model, t.severity) == ("not_null_fact_daily_prices_close", "unknown", None,
                                                               "error")


def test_load_run_results_missing_file(tmp_path):
    assert rep.load_run_results(tmp_path) is None


def test_target_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BVT_DBT_DIR", str(tmp_path))
    monkeypatch.delenv("DBT_TARGET_PATH", raising=False)
    assert rep.target_dir() == tmp_path / "target"
    monkeypatch.setenv("DBT_TARGET_PATH", "out")
    assert rep.target_dir() == tmp_path / "out"
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path / "abs"))
    assert rep.target_dir() == tmp_path / "abs"


class RecordingConn:
    def __init__(self):
        self.rows, self.sql, self.commits = [], None, 0

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def executemany(self, sql, rows):
                conn.sql, conn.rows = sql, list(rows)

        return Cur()

    def commit(self):
        self.commits += 1


def test_record_results_upserts_every_test(tmp_path):
    run = rep.load_run_results(write_target(tmp_path, RESULTS, NODES))
    conn = RecordingConn()
    assert rep.record_results(conn, run) == 4
    assert "ON CONFLICT (invocation_id, unique_id) DO UPDATE" in conn.sql
    assert conn.commits == 1
    first = conn.rows[0]
    assert first[0] == "inv-1" and first[2] == run.generated_at
    assert {r[8] for r in conn.rows} == {"pass", "fail", "warn", "error"}
    assert {r[7] for r in conn.rows} <= {"error", "warn"}


def test_record_latest_skips_stale_or_missing_results(tmp_path, monkeypatch):
    import bvt.db

    monkeypatch.setattr(bvt.db, "connect", lambda: pytest.fail("bağlanmamalıydı"))
    assert rep.record_latest(target=tmp_path) == 0  # dosya yok
    write_target(tmp_path, RESULTS, NODES)
    later = datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc)
    assert rep.record_latest(started_after=later, target=tmp_path) == 0  # önceki çağrının dosyası


def test_record_latest_writes_fresh_results(tmp_path, monkeypatch):
    import contextlib

    import bvt.db

    conn = RecordingConn()
    monkeypatch.setattr(bvt.db, "connect", contextlib.contextmanager(lambda: (yield conn)))
    write_target(tmp_path, RESULTS, NODES)
    earlier = datetime(2026, 11, 26, 16, 40, tzinfo=timezone.utc)
    assert rep.record_latest(started_after=earlier, target=tmp_path) == 4
    assert len(conn.rows) == 4


def _tests(tmp_path):
    return rep.load_run_results(write_target(tmp_path, RESULTS, NODES)).tests


ANOMALIES = [
    {"instrument_code": "THYAO", "anomaly_type": "return_spike", "severity": "error", "n_days": 1,
     "last_date": date(2026, 11, 25), "max_abs_metric": 0.31},
    {"instrument_code": "USDTRY", "anomaly_type": "stale_price", "severity": "warn", "n_days": 3,
     "last_date": date(2026, 11, 20), "max_abs_metric": 7.0},
]


def test_summarize_counts_statuses_and_anomalies(tmp_path):
    summary = rep.summarize(_tests(tmp_path), ANOMALIES)
    assert summary == {"pass": 1, "warn": 1, "fail": 1, "error": 1, "skipped": 0, "total": 4, "ok": False,
                       "anomalies": {"error": 1, "warn": 3}}
    assert rep.summarize([])["ok"] is True


def test_render_markdown_lists_problems_worst_first(tmp_path):
    md = rep.render_markdown(_tests(tmp_path), ANOMALIES, datetime(2026, 11, 26, 16, 45, tzinfo=timezone.utc),
                             "test")
    assert md.startswith("# dbt test raporu")
    assert "2026-11-26 19:45 (Europe/Istanbul)" in md
    assert "**Durum: HATA" in md
    problems = md.split("## Başarısız / uyarı veren testler")[1].split("## Model başına")[0]
    order = [line.split("|")[1].strip() for line in problems.splitlines() if line.startswith("| ")][1:]
    assert order == ["error", "fail", "warn"]
    assert "| fail | assert_gram_gold_matches_ounce | singular | stg_gold_prices | - | 3 |" in md
    assert "| error | THYAO | return_spike | 1 | 2026-11-25 | 0.31 |" in md
    assert "not_null_fact_daily_prices_close" not in problems  # geçen test sorun listesinde yok


def test_render_markdown_clean_and_missing_anomalies():
    passing = [rep.TestResult("test.bvt.x", "x", "not_null", "m", "c", "error", "pass", 0, None, 0.1)]
    md = rep.render_markdown(passing, [], datetime(2026, 11, 26, tzinfo=timezone.utc), "test")
    assert "**Durum: TEMİZ" in md and md.count("Yok.") == 2
    md = rep.render_markdown([], None, datetime(2026, 11, 26, tzinfo=timezone.utc), "test")
    assert "VERİ YOK" in md and "okunamadı" in md


def test_render_markdown_escapes_and_truncates_messages():
    long = "a|b " + "x" * 500
    t = rep.TestResult("test.bvt.y", "y", "singular", None, None, "error", "fail", 1, long, None)
    row = next(line for line in rep.render_markdown([t], [], datetime.now(timezone.utc), "s").splitlines()
               if line.startswith("| fail | y "))
    assert "a\\|b" in row and row.endswith("… |")
    assert len(row) < rep.MESSAGE_MAX_CHARS + 100


def test_warn_or_error_anomaly_gives_warning_status():
    passing = [rep.TestResult("test.bvt.x", "x", "not_null", "m", "c", "error", "pass", 0, None, 0.1)]
    md = rep.render_markdown(passing, ANOMALIES[:1], datetime.now(timezone.utc), "s")
    assert "**Durum: UYARI" in md


class ReportConn:
    """daily_report için sahte bağlantı: LATEST_SQL ve ANOMALY_SQL sonuçlarını döner."""

    def __init__(self, tests, anomalies):
        self.tests, self.anomalies, self.params = tests, anomalies, []

    def execute(self, sql, params=None):
        self.params.append(params)
        conn = self

        class Cur:
            names = ANOMALIES[0] if "mart_price_anomalies" in sql else {}
            description = [type("D", (), {"name": k})() for k in names]

            def fetchall(self):
                if "mart_price_anomalies" in sql:
                    return [tuple(a.values()) for a in conn.anomalies]
                return [tuple(vars(t).values()) for t in conn.tests]

        return Cur()


def test_daily_report_writes_markdown_and_json(tmp_path, monkeypatch):
    import contextlib

    import bvt.db

    conn = ReportConn(_tests(tmp_path), ANOMALIES)
    monkeypatch.setattr(bvt.db, "connect", contextlib.contextmanager(lambda: (yield conn)))
    now = datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc)
    out = tmp_path / "report"
    summary = rep.daily_report(hours=6, anomaly_days=30, out_dir=out, now=now)
    assert conn.params == [(now - timedelta(hours=6),), (date(2026, 10, 27),)]
    assert summary["ok"] is False and summary["fail"] == 1 and summary["anomalies"] == {"error": 1, "warn": 3}
    assert summary["report"] == str(out / rep.REPORT_MD)
    assert "assert_gram_gold_matches_ounce" in (out / rep.REPORT_MD).read_text(encoding="utf-8")
    payload = json.loads((out / rep.REPORT_JSON).read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 4 and len(payload["problems"]) == 3
    assert payload["anomalies"][0]["last_date"] == "2026-11-25"


def test_report_from_target_without_database(tmp_path):
    write_target(tmp_path, RESULTS, NODES)
    summary = rep.report_from_target(target=tmp_path, out_dir=tmp_path / "r")
    assert summary["total"] == 4
    assert "dbt çağrısı inv-1" in (tmp_path / "r" / rep.REPORT_MD).read_text(encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        rep.report_from_target(target=tmp_path / "yok")


def test_main_from_target_and_fail_on_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bvt.logging_setup.setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("BVT_DBT_DIR", str(tmp_path))
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path))
    write_target(tmp_path, RESULTS, NODES)
    assert rep.main(["--from-target", "--output", str(tmp_path / "o")]) == 0
    assert "# dbt test raporu" in capsys.readouterr().out
    assert rep.main(["--from-target", "--output", str(tmp_path / "o"), "--fail-on-error"]) == 1
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path / "bos"))
    assert rep.main(["--from-target"]) == 1
