"""dbt test sonuçlarını kaydeder ve günlük test raporu üretir.

İki adım:

1. **Kayıt** (`record_latest`): her `dbt build` sonrasında (`bvt.dbt_runner.build_staging/build_marts`, başarılı
   ya da başarısız) `target/run_results.json` okunur, test düğümlerinin sonuçları `manifest.json`'daki bilgilerle
   (test türü, bağlı model/sütun, önem derecesi) zenginleştirilip `ops.dbt_test_results`'a yazılır. dbt her
   çağrıda run_results.json'ı ezdiğinden kayıt çağrı başına yapılır; dosya çağrıdan eskiyse yazılmaz.
2. **Rapor** (`daily_report`): son saatlerdeki çağrılarda her testin en güncel sonucu ve son 30 günün fiyat
   anormallikleri (`marts.mart_price_anomalies`) Markdown (`test_report.md`) ve JSON (`test_report.json`) olarak
   yazılır. Varsayılan dizin dbt hedef dizinidir (`DBT_TARGET_PATH`, yoksa `dbt/target`); `BVT_REPORT_DIR` ile
   değiştirilebilir. Özet sözlüğü Airflow XCom'una döner; alarm (Gün 13) bunu kullanır.

Kullanım:
    python -m bvt.dbt_report                       # veritabanından günlük rapor (son 6 saat + 30 günlük anormallik)
    python -m bvt.dbt_report --record              # son run_results.json'ı ops.dbt_test_results'a yaz, sonra rapor
    python -m bvt.dbt_report --from-target         # veritabanı olmadan, yalnızca son run_results.json'dan rapor
    python -m bvt.dbt_report --fail-on-error       # başarısız test varsa çıkış kodu 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("bvt.dbt_report")

STATUSES = ("pass", "warn", "fail", "error", "skipped")
PROBLEM_STATUSES = ("error", "fail", "warn")  # rapordaki sıralama: en ciddi önce
REPORT_WINDOW_HOURS = 6       # günlük DAG'deki staging + marts build'leri bu pencereye sığar
ANOMALY_DAYS = 30
MESSAGE_MAX_CHARS = 200
REPORT_MD = "test_report.md"
REPORT_JSON = "test_report.json"
TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True)
class TestResult:
    unique_id: str
    test_name: str
    test_type: str
    model: str | None
    column_name: str | None
    severity: str
    status: str
    failures: int | None
    message: str | None
    execution_time_s: float | None


@dataclass(frozen=True)
class RunResults:
    invocation_id: str
    generated_at: datetime
    tests: list[TestResult]


# ---------------------------------------------------------------------------------------------- run_results.json


def target_dir() -> Path:
    """dbt hedef dizini: `DBT_TARGET_PATH` (göreliyse dbt proje dizinine göre) > `<dbt>/target`."""
    from bvt.dbt_runner import dbt_dir

    explicit = os.getenv("DBT_TARGET_PATH")
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else dbt_dir() / path
    return dbt_dir() / "target"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status(value: str | None) -> str:
    status = (value or "").strip().lower()
    return status if status in STATUSES else "error"  # ör. "runtime error"


def _attached_model(node: dict) -> str | None:
    """`model.bvt.fact_daily_prices` → `fact_daily_prices`, `source.bvt.raw.fx_rates` → `raw.fx_rates`."""
    candidates = [node.get("attached_node")] + list(node.get("depends_on", {}).get("nodes", []))
    for uid in candidates:
        if not uid:
            continue
        kind, _, rest = uid.partition(".")
        _project, _, name = rest.partition(".")
        if kind in ("model", "seed", "snapshot", "source") and name:
            return name
    return None


def parse_test(result: dict, node: dict | None) -> TestResult:
    """run_results.json'daki bir sonuç + manifest düğümü → TestResult (manifest yoksa unique_id'den türetilir)."""
    uid = result["unique_id"]
    node = node or {}
    failures = result.get("failures")
    execution_time = result.get("execution_time")
    message = result.get("message")
    return TestResult(
        unique_id=uid,
        test_name=node.get("name") or (uid.split(".")[2] if uid.count(".") >= 2 else uid),
        test_type=(node.get("test_metadata") or {}).get("name") or ("singular" if node else "unknown"),
        model=_attached_model(node),
        column_name=node.get("column_name"),
        severity=str((node.get("config") or {}).get("severity") or "error").lower(),
        status=_status(result.get("status")),
        failures=int(failures) if failures is not None else None,
        message=" ".join(str(message).split()) if message else None,
        execution_time_s=float(execution_time) if execution_time is not None else None,
    )


def load_run_results(target: Path | None = None) -> RunResults | None:
    """Son dbt çağrısının test sonuçları; run_results.json yoksa None. Model/seed sonuçları atlanır."""
    target = target or target_dir()
    path = target / "run_results.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    manifest_path = target / "manifest.json"
    nodes = {}
    if manifest_path.is_file():
        nodes = json.loads(manifest_path.read_text(encoding="utf-8")).get("nodes", {})
    meta = data.get("metadata", {})
    tests = [parse_test(r, nodes.get(r["unique_id"])) for r in data.get("results", [])
             if str(r.get("unique_id", "")).startswith("test.")]
    return RunResults(invocation_id=meta.get("invocation_id") or "unknown",
                      generated_at=_parse_time(meta["generated_at"]), tests=tests)


# ------------------------------------------------------------------------------------------------ veritabanı

INSERT_SQL = """
INSERT INTO ops.dbt_test_results (invocation_id, unique_id, generated_at, test_name, test_type, model, column_name,
                                  severity, status, failures, message, execution_time_s)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (invocation_id, unique_id) DO UPDATE SET
    status = EXCLUDED.status, failures = EXCLUDED.failures, message = EXCLUDED.message,
    execution_time_s = EXCLUDED.execution_time_s, recorded_at = now()
"""

LATEST_SQL = """
SELECT DISTINCT ON (unique_id)
       unique_id, test_name, test_type, model, column_name, severity, status, failures, message, execution_time_s
  FROM ops.dbt_test_results
 WHERE generated_at >= %s
 ORDER BY unique_id, generated_at DESC
"""

ANOMALY_SQL = """
SELECT instrument_code, anomaly_type, severity, count(*) AS n_days, max(price_date) AS last_date,
       max(abs(metric_value)) AS max_abs_metric
  FROM marts.mart_price_anomalies
 WHERE price_date >= %s
 GROUP BY instrument_code, anomaly_type, severity
 ORDER BY severity, max(price_date) DESC, instrument_code, anomaly_type
"""


def record_results(conn, run: RunResults) -> int:
    """Test sonuçlarını ops.dbt_test_results'a yazar (aynı çağrı tekrar kaydedilirse günceller)."""
    severity = lambda s: s if s in ("error", "warn") else "error"  # noqa: E731
    rows = [(run.invocation_id, t.unique_id, run.generated_at, t.test_name, t.test_type, t.model, t.column_name,
             severity(t.severity), t.status, t.failures, t.message, t.execution_time_s) for t in run.tests]
    if rows:
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)
        conn.commit()
    return len(rows)


def record_latest(started_after: datetime | None = None, target: Path | None = None) -> int:
    """Son run_results.json'ı kaydeder. Dosya yoksa ya da `started_after`'dan eskiyse (dbt sonuç yazamadan düştü)
    hiçbir şey yazmaz. Yazılan test sayısını döner."""
    run = load_run_results(target)
    if run is None:
        log.warning("run_results.json bulunamadı; test sonuçları kaydedilmedi")
        return 0
    if started_after is not None and run.generated_at < started_after:
        log.warning("run_results.json bu çağrıdan eski (%s); test sonuçları kaydedilmedi", run.generated_at)
        return 0
    if not run.tests:
        return 0
    from bvt.db import connect

    with connect() as conn:
        n = record_results(conn, run)
    counts = Counter(t.status for t in run.tests)
    log.info("dbt test sonuçları kaydedildi: %d test %s", n, dict(counts),
             extra={"invocation_id": run.invocation_id, "test_counts": dict(counts)})
    return n


def fetch_latest(conn, since: datetime) -> list[TestResult]:
    """`since`'ten sonraki çağrılarda her testin en güncel sonucu."""
    rows = conn.execute(LATEST_SQL, (since,)).fetchall()
    return [TestResult(*row) for row in rows]


def fetch_anomalies(conn, since: date) -> list[dict] | None:
    """Enstrüman × kural başına anormallik özeti; marts henüz kurulmadıysa None."""
    import psycopg

    try:
        cur = conn.execute(ANOMALY_SQL, (since,))
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None
    columns = [d.name for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# ------------------------------------------------------------------------------------------------------ rapor


def summarize(tests: list[TestResult], anomalies: list[dict] | None = None) -> dict:
    counts = Counter(t.status for t in tests)
    summary = {status: counts.get(status, 0) for status in STATUSES}
    summary["total"] = len(tests)
    summary["ok"] = summary["fail"] == 0 and summary["error"] == 0
    if anomalies is not None:
        by_severity = Counter()
        for row in anomalies:
            by_severity[row["severity"]] += int(row["n_days"])
        summary["anomalies"] = {"error": by_severity.get("error", 0), "warn": by_severity.get("warn", 0)}
    return summary


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    text = " ".join(str(value).split()).replace("|", "\\|")
    return text if len(text) <= MESSAGE_MAX_CHARS else text[: MESSAGE_MAX_CHARS - 1] + "…"


def _overall(summary: dict) -> str:
    if not summary["ok"]:
        return "HATA: başarısız test var"
    if summary["warn"] or summary.get("anomalies", {}).get("error"):
        return "UYARI: gözden geçirilecek kayıt var"
    if summary["total"] == 0:
        return "VERİ YOK: pencerede kayıtlı test sonucu yok"
    return "TEMİZ: tüm testler geçti"


def render_markdown(tests: list[TestResult], anomalies: list[dict] | None, generated_at: datetime,
                    scope: str, anomaly_days: int = ANOMALY_DAYS) -> str:
    summary = summarize(tests, anomalies)
    local = generated_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# dbt test raporu",
        "",
        f"Oluşturulma: {local} (Europe/Istanbul) · Kapsam: {scope}",
        "",
        f"**Durum: {_overall(summary)}**",
        "",
        "| Durum | Adet |",
        "|---|---:|",
        *(f"| {status} | {summary[status]} |" for status in STATUSES),
        f"| **toplam** | **{summary['total']}** |",
        "",
    ]

    problems = sorted((t for t in tests if t.status in PROBLEM_STATUSES),
                      key=lambda t: (PROBLEM_STATUSES.index(t.status), t.model or "", t.test_name))
    lines += ["## Başarısız / uyarı veren testler", ""]
    if problems:
        lines += ["| Durum | Test | Tür | Model | Sütun | Satır | Mesaj |", "|---|---|---|---|---|---:|---|"]
        lines += [f"| {t.status} | {_cell(t.test_name)} | {t.test_type} | {_cell(t.model)} | {_cell(t.column_name)} "
                  f"| {_cell(t.failures)} | {_cell(t.message)} |" for t in problems]
    else:
        lines.append("Yok.")
    lines.append("")

    by_model = Counter(t.model or "-" for t in tests)
    failing_by_model = Counter(t.model or "-" for t in problems)
    if by_model:
        lines += ["## Model başına", "", "| Model | Test | Sorunlu |", "|---|---:|---:|"]
        lines += [f"| {model} | {n} | {failing_by_model.get(model, 0)} |" for model, n in sorted(by_model.items())]
        lines.append("")

    lines += [f"## Fiyat anormallikleri (son {anomaly_days} gün)", ""]
    if anomalies is None:
        lines.append("`marts.mart_price_anomalies` okunamadı (marts henüz kurulmadı ya da veritabanı yok).")
    elif not anomalies:
        lines.append("Yok.")
    else:
        lines += ["| Önem | Enstrüman | Kural | Gün | Son gün | En büyük \\|değer\\| |", "|---|---|---|---:|---|---:|"]
        lines += [f"| {a['severity']} | {a['instrument_code']} | {a['anomaly_type']} | {a['n_days']} "
                  f"| {a['last_date']} | {_cell(None if a['max_abs_metric'] is None else float(a['max_abs_metric']))} |"
                  for a in anomalies]
        lines += ["", "Kurallar: return_spike (|getiri| > varlık eşiği), zscore_spike (|z| > 5), stale_price "
                      "(≥ 5 gün aynı fiyat), zero_volume, invalid_ohlc. Ayrıntı: `src/bvt/anomalies.py`."]
    lines.append("")
    return "\n".join(lines)


def report_dir() -> Path:
    explicit = os.getenv("BVT_REPORT_DIR")
    return Path(explicit) if explicit else target_dir()


def write_report(markdown: str, payload: dict, out_dir: Path | None = None) -> Path:
    """Markdown ve JSON raporunu yazar; Markdown dosyasının yolunu döner."""
    out_dir = out_dir or report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / REPORT_MD
    md_path.write_text(markdown, encoding="utf-8")
    (out_dir / REPORT_JSON).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                                       encoding="utf-8")
    return md_path


def _payload(summary: dict, tests: list[TestResult], anomalies: list[dict] | None, generated_at: datetime) -> dict:
    return {"generated_at": generated_at.isoformat(), "summary": summary,
            "problems": [asdict(t) for t in tests if t.status in PROBLEM_STATUSES], "anomalies": anomalies}


def daily_report(hours: int = REPORT_WINDOW_HOURS, anomaly_days: int = ANOMALY_DAYS,
                 out_dir: Path | None = None, now: datetime | None = None) -> dict:
    """Airflow görevinin çağırdığı fonksiyon: son `hours` saatteki test sonuçları + anormallikler → rapor dosyası.
    Görev testler başarısız olsa da düşmez (rapor her durumda üretilir); özet XCom'a döner."""
    from bvt.db import connect

    now = now or datetime.now(timezone.utc)
    with connect() as conn:
        tests = fetch_latest(conn, now - timedelta(hours=hours))
        anomalies = fetch_anomalies(conn, now.astimezone(TZ).date() - timedelta(days=anomaly_days))
    summary = summarize(tests, anomalies)
    markdown = render_markdown(tests, anomalies, now, f"son {hours} saatteki dbt çağrıları", anomaly_days)
    path = write_report(markdown, _payload(summary, tests, anomalies, now), out_dir)
    log.info("dbt test raporu yazıldı: %s (%s)", path, _overall(summary), extra={"report_summary": summary})
    return {**summary, "report": str(path)}


def report_from_target(target: Path | None = None, out_dir: Path | None = None) -> dict:
    """Veritabanı olmadan, yalnızca son run_results.json'dan rapor (yerel geliştirme)."""
    run = load_run_results(target)
    if run is None:
        raise FileNotFoundError(f"run_results.json yok: {(target or target_dir())}; önce `dbt build` çalıştırın")
    summary = summarize(run.tests)
    markdown = render_markdown(run.tests, None, run.generated_at, f"dbt çağrısı {run.invocation_id}")
    path = write_report(markdown, _payload(summary, run.tests, None, run.generated_at), out_dir)
    return {**summary, "report": str(path)}


def main(argv: list[str] | None = None) -> int:
    from bvt.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="dbt test sonuçlarını kaydeder ve test raporu üretir.")
    parser.add_argument("--record", action="store_true", help="önce son run_results.json'ı veritabanına yaz")
    parser.add_argument("--from-target", action="store_true", help="veritabanı yerine son run_results.json'dan")
    parser.add_argument("--hours", type=int, default=REPORT_WINDOW_HOURS, help="rapora girecek çağrı penceresi")
    parser.add_argument("--anomaly-days", type=int, default=ANOMALY_DAYS)
    parser.add_argument("--output", type=Path, help="rapor dizini (varsayılan: dbt hedef dizini)")
    parser.add_argument("--fail-on-error", action="store_true", help="başarısız test varsa çıkış kodu 1")
    args = parser.parse_args(argv)
    setup_logging()
    import psycopg

    try:
        if args.from_target:
            summary = report_from_target(out_dir=args.output)
        else:
            if args.record:
                record_latest()
            summary = daily_report(args.hours, args.anomaly_days, args.output)
    except (OSError, ValueError, psycopg.Error) as exc:  # dosya / bağlantı / sorgu hatası
        log.error("%s", exc)
        return 1
    print(Path(summary["report"]).read_text(encoding="utf-8"))
    return 1 if args.fail_on_error and not summary["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
