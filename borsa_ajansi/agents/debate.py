"""2. Kademe: Boğa vs. Ayı çapraz sorgusu (güçlü model). Dayanaksız argümanlar programatik olarak elenir."""
from __future__ import annotations

import json
from typing import Any, Literal

from agents.analysts import raw_data_for
from agents.llm import LLM
from config import prompts
from verification.fact_checker import _is_num, _within, flatten
from verification.schemas import AnalystReport, Argument, DebateTurn, FactCheckResult, ValidatedTurn

Side = Literal["boga", "ayi"]


def validate_argument(arg: Argument, bundle: dict[str, Any], reports: dict[str, AnalystReport],
                      checks: dict[str, FactCheckResult]) -> str | None:
    """None -> kabul; str -> eleme gerekçesi."""
    src = arg.source_analyst
    if src not in reports:
        return "kaynak analist raporu yok"
    if src in checks and not checks[src].passed:
        return f"kaynak '{src}' raporu doğrulamadan geçemedi"
    if arg.evidence_field == "key_points":
        if not reports[src].key_points:
            return "kaynak raporda key_points yok"
        return None
    flat = flatten(raw_data_for(src, bundle))
    if arg.evidence_field not in flat:
        return f"'{arg.evidence_field}' {src} ham verisinde yok"
    actual = flat[arg.evidence_field]
    if not _is_num(actual):
        if arg.evidence_value is not None:
            return f"'{arg.evidence_field}' ham değeri '{actual}', sayısal iddia dayanaksız"
        return None
    if arg.evidence_value is None:
        return f"'{arg.evidence_field}' sayısal; evidence_value verilmeli"
    ok, rule = _within(arg.evidence_field, arg.evidence_value, float(actual))
    return None if ok else f"'{arg.evidence_field}': iddia={arg.evidence_value}, gerçek={actual} ({rule})"


def validate_turn(turn: DebateTurn, bundle, reports, checks) -> ValidatedTurn:
    accepted, rejected = [], []
    for a in turn.arguments:
        reason = validate_argument(a, bundle, reports, checks)
        if reason is None:
            accepted.append(a)
        else:
            rejected.append(f"{a.claim} -> ELENDİ: {reason}")
    return ValidatedTurn(turn=turn, accepted=accepted, rejected=rejected)


def _reports_payload(reports: dict[str, AnalystReport], checks: dict[str, FactCheckResult]) -> list[dict]:
    out = []
    for name, r in reports.items():
        d = r.model_dump()
        d["fact_check_passed"] = checks[name].passed if name in checks else False
        out.append(d)
    return out


def build_debate_prompt(side: Side, rnd: int, ticker: str, bundle, reports, checks,
                        history: list[ValidatedTurn]) -> str:
    hist = [{"side": v.turn.side, "round": v.turn.round, "thesis": v.turn.thesis,
             "accepted_arguments": [a.model_dump() for a in v.accepted],
             "rejected": v.rejected, "rebuttals": v.turn.rebuttals} for v in history]
    raw = {name: raw_data_for(name, bundle) for name in reports}
    return (
        f"Hisse: {ticker}\nTur: {rnd}\nTaraf: {side}\n"
        f"Doğrulanmış analist raporları:\n{json.dumps(_reports_payload(reports, checks), ensure_ascii=False, default=str)}\n\n"
        f"Ham veri (anahtar adları için):\n{json.dumps(raw, ensure_ascii=False, default=str)}\n\n"
        f"Tartışma geçmişi:\n{json.dumps(hist, ensure_ascii=False, default=str)}"
    )


def run_turn(llm: LLM, side: Side, rnd: int, ticker: str, bundle, reports, checks,
             history: list[ValidatedTurn]) -> ValidatedTurn:
    system = prompts.BULL_AGENT if side == "boga" else prompts.BEAR_AGENT
    turn = llm.with_structured_output(DebateTurn, tier="reasoning").invoke(
        system, build_debate_prompt(side, rnd, ticker, bundle, reports, checks, history))
    turn = turn.model_copy(update={"side": side, "round": rnd})
    return validate_turn(turn, bundle, reports, checks)


def needs_extra_round(history: list[ValidatedTurn]) -> bool:
    """Son turda bir taraf hiç geçerli argüman üretemediyse bir tur daha hak tanınır (max limit dahilinde)."""
    last = history[-2:]
    return any(not v.accepted for v in last)
