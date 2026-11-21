"""3. Kademe: Risk & Güven Eşiği Kapısı.

LLM (hakem) nitel kararı verir; güven düşürme, eşik kapısı, çelişki kapısı, giriş/stop/hedef ve
risk/ödül oranı tamamen Python'da deterministik hesaplanır.
"""
from __future__ import annotations

import json
from typing import Any

from agents.analysts import raw_data_for
from agents.llm import LLM
from config import prompts
from config.settings import VERI_YOK, settings
from verification.fact_checker import _is_num
from verification.schemas import (AnalystReport, FactCheckResult, FinalDecision, RiskJudgement, TradePlan,
                                  ValidatedTurn)

MIN_RISK_REWARD = 1.5
CONFLICT_MIN_CONFIDENCE = 0.6


def compute_trade_plan(decision: str, ind: dict[str, Any]) -> TradePlan | None:
    """AL/SAT için giriş bölgesi, stop, hedef ve R/Ö — ATR + destek/direnç tabanlı."""
    c, a = ind.get("close"), ind.get("atr14")
    if not (_is_num(c) and _is_num(a)) or a <= 0:
        return None
    lv = ind.get("levels", {}) or {}
    s20, r20 = lv.get("support_20d"), lv.get("resistance_20d")
    s60, r60 = lv.get("support_60d"), lv.get("resistance_60d")

    if decision == "AL":
        lo, hi = c - 0.5 * a, c
        if _is_num(s20) and c - 3 * a <= s20 < c - 0.5 * a:
            stop, sb = s20 - 0.25 * a, "20g destek altı"
        else:
            stop, sb = c - 2 * a, "2xATR"
        if _is_num(r60) and r60 >= c + 1.5 * a:
            tgt, tb = r60, "60g direnç"
        else:
            tgt, tb = c + 3 * a, "3xATR"
        mid = (lo + hi) / 2
        rr = (tgt - mid) / (mid - stop)
    elif decision == "SAT":
        lo, hi = c, c + 0.5 * a
        if _is_num(r20) and c + 0.5 * a < r20 <= c + 3 * a:
            stop, sb = r20 + 0.25 * a, "20g direnç üstü"
        else:
            stop, sb = c + 2 * a, "2xATR"
        if _is_num(s60) and s60 <= c - 1.5 * a:
            tgt, tb = s60, "60g destek"
        else:
            tgt, tb = c - 3 * a, "3xATR"
        mid = (lo + hi) / 2
        rr = (mid - tgt) / (stop - mid)
    else:
        return None
    r = lambda x: round(float(x), 4)
    return TradePlan(entry_low=r(lo), entry_high=r(hi), stop_loss=r(stop), target=r(tgt),
                     risk_reward=round(float(rr), 2), basis=f"stop: {sb}, hedef: {tb}, ATR14={a}")


def confidence_penalties(bundle, reports: dict[str, AnalystReport], checks: dict[str, FactCheckResult],
                         debate: list[ValidatedTurn]) -> tuple[float, list[str]]:
    pen, reasons = 0.0, []
    for name, rep in reports.items():
        raw = raw_data_for(name, bundle)
        if raw.get("status") == VERI_YOK or rep.data_date == VERI_YOK:
            pen += settings.veri_yok_confidence_penalty
            reasons.append(f"{name}: veri_yok (-{settings.veri_yok_confidence_penalty:.2f})")
        elif name in checks and not checks[name].passed:
            pen += settings.veri_yok_confidence_penalty
            reasons.append(f"{name}: fact-check geçilemedi (-{settings.veri_yok_confidence_penalty:.2f})")
    total = sum(len(v.turn.arguments) for v in debate)
    rejected = sum(len(v.rejected) for v in debate)
    if total and rejected / total > 0.5:
        pen += 0.05
        reasons.append(f"tartışmada argümanların %{rejected / total * 100:.0f}'i elendi (-0.05)")
    return pen, reasons


def detect_conflict(reports: dict[str, AnalystReport]) -> bool:
    t, f = reports.get("teknik"), reports.get("temel")
    if not t or not f:
        return False
    opposite = {t.signal, f.signal} == {"olumlu", "olumsuz"}
    return opposite and min(t.confidence, f.confidence) >= CONFLICT_MIN_CONFIDENCE


def build_risk_prompt(ticker, reports, checks, debate, penalties_reasons) -> str:
    payload = {
        "ticker": ticker,
        "analyst_reports": [{**r.model_dump(), "fact_check_passed": checks[n].passed if n in checks else False}
                            for n, r in reports.items()],
        "debate": [{"side": v.turn.side, "round": v.turn.round, "thesis": v.turn.thesis,
                    "accepted": [a.model_dump() for a in v.accepted], "rejected": v.rejected,
                    "rebuttals": v.turn.rebuttals} for v in debate],
        "data_quality_issues": penalties_reasons,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def decide(llm: LLM, ticker: str, bundle, reports, checks, debate) -> FinalDecision:
    pen, pen_reasons = confidence_penalties(bundle, reports, checks, debate)
    judgement = llm.with_structured_output(RiskJudgement, tier="reasoning").invoke(
        prompts.RISK_MANAGER, build_risk_prompt(ticker, reports, checks, debate, pen_reasons))
    return apply_gates(ticker, bundle, reports, judgement, pen, pen_reasons)


def apply_gates(ticker, bundle, reports, j: RiskJudgement, penalty: float, pen_reasons: list[str]) -> FinalDecision:
    ind = bundle.get("market", {}).get("indicators", {})
    conf = max(0.0, round(j.confidence - penalty, 3))
    gates = list(pen_reasons)
    decision = j.decision
    plan = None

    if decision != "BEKLE":
        if conf < settings.confidence_threshold:
            gates.append(f"güven %{conf * 100:.0f} < eşik %{settings.confidence_threshold * 100:.0f} -> BEKLE")
            decision = "BEKLE"
        elif j.technical_fundamental_conflict or detect_conflict(reports):
            gates.append("teknik/temel arasında sert çelişki -> BEKLE")
            decision = "BEKLE"
    if decision != "BEKLE":
        plan = compute_trade_plan(decision, ind)
        if plan is None:
            gates.append("fiyat/ATR verisi yok, seviye üretilemedi -> BEKLE")
            decision = "BEKLE"
        elif plan.risk_reward < MIN_RISK_REWARD:
            gates.append(f"risk/ödül {plan.risk_reward} < {MIN_RISK_REWARD} -> BEKLE")
            decision, plan = "BEKLE", None

    close = ind.get("close")
    return FinalDecision(
        ticker=ticker, data_date=ind.get("date", VERI_YOK), decision=decision, llm_decision=j.decision,
        raw_confidence=j.confidence, confidence=conf, winning_side=j.winning_side, rationale=j.rationale,
        key_risks=j.key_risks, gate_reasons=gates, trade_plan=plan, close=close if _is_num(close) else None,
    )
