"""1. Kademe: analist JSON'undaki sayısal iddiaları ham veriyle PROGRAMATİK olarak karşılaştırır (LLM yok)."""
from __future__ import annotations

import re
from typing import Any

from config.settings import VERI_YOK, settings
from verification.schemas import AnalystReport, ClaimCheck, FactCheckResult

_PCT_FIELD = re.compile(r"_pct$")
_OSC_FIELDS = {"rsi14", "stoch_k"}
_RATIO_FIELDS = {"pe_ratio", "forward_pe", "pb_ratio", "ev_ebitda", "net_debt_to_ebitda", "volume_ratio",
                 "debt_to_equity", "bollinger_position"}
_COUNT_FIELDS = {"news_count"}


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _within(field: str, claimed: float, actual: float) -> tuple[bool, str]:
    name = field.split(".")[-1]
    diff = abs(claimed - actual)
    if name in _COUNT_FIELDS:
        return diff < 0.5, "tam eşleşme gerekli"
    if name in _OSC_FIELDS:
        tol = settings.indicator_tolerance
        return diff <= tol, f"±{tol} mutlak"
    if _PCT_FIELD.search(name):
        return diff <= 0.5, "±0.5 puan"
    rel_tol = settings.ratio_tolerance_pct if name in _RATIO_FIELDS else settings.price_tolerance_pct
    if actual == 0:
        return diff < 1e-9, "sıfır"
    return diff / abs(actual) * 100 <= rel_tol, f"±%{rel_tol}"


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_report(report: AnalystReport, raw: dict[str, Any], expected_ticker: str | None = None,
                 attempts: int = 1) -> FactCheckResult:
    flat = flatten(raw)
    errors: list[str] = []
    checks: list[ClaimCheck] = []

    if expected_ticker and report.ticker != expected_ticker:
        errors.append(f"ticker uyuşmazlığı: rapor={report.ticker}, beklenen={expected_ticker}")

    raw_date = flat.get("date", VERI_YOK)
    if raw_date != VERI_YOK and report.data_date != raw_date:
        errors.append(f"tarih uyuşmazlığı: rapor={report.data_date}, ham veri={raw_date}")
    if raw_date == VERI_YOK and report.data_date not in (VERI_YOK, ""):
        errors.append(f"ham veride tarih yok ama rapor '{report.data_date}' iddia ediyor")

    for c in report.claims:
        if c.field not in flat:
            checks.append(ClaimCheck(field=c.field, claimed=c.value, actual=None, ok=False, reason="ham veride böyle alan yok"))
            errors.append(f"'{c.field}' alanı ham veride yok (iddia: {c.value})")
            continue
        actual = flat[c.field]
        if not _is_num(actual):
            checks.append(ClaimCheck(field=c.field, claimed=c.value, actual=str(actual), ok=False, reason="ham değer sayısal değil / veri_yok"))
            errors.append(f"'{c.field}' ham veride '{actual}' ama rapor {c.value} iddia ediyor (halüsinasyon)")
            continue
        ok, rule = _within(c.field, c.value, float(actual))
        checks.append(ClaimCheck(field=c.field, claimed=c.value, actual=float(actual), ok=ok, reason="" if ok else rule))
        if not ok:
            errors.append(f"'{c.field}': rapor={c.value}, gerçek={actual} (tolerans {rule})")

    return FactCheckResult(analyst=report.analyst, passed=not errors, checks=checks, errors=errors, attempts=attempts)
