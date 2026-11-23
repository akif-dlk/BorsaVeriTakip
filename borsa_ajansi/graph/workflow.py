"""LangGraph orkestrasyonu: veri -> 4 analist (paralel) -> fact-check döngüsü -> boğa/ayı -> risk kapısı.

Döngü limitleri:
- Fact-check: her analist en fazla 1 + MAX_FACT_CHECK_RETRIES (=2) kez çalışır.
- Tartışma: en az MIN_DEBATE_ROUNDS, en fazla MAX_DEBATE_ROUNDS tur.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from agents import analysts as analysts_mod
from agents import debate as debate_mod
from agents import risk_manager
from agents.llm import LLM, LLMError
from config.settings import settings
from tools import fundamental_data, market_data, news_sentiment
from verification import fact_checker
from verification.schemas import AnalystReport, FactCheckResult, FinalDecision, ValidatedTurn

log = logging.getLogger(__name__)
ProgressFn = Callable[[str], None]


class AnalysisState(TypedDict, total=False):
    ticker: str
    bundle: dict[str, Any]
    reports: dict[str, AnalystReport]
    checks: dict[str, FactCheckResult]
    attempts: dict[str, int]
    pending: list[str]
    feedback: dict[str, list[str]]
    debate: list[ValidatedTurn]
    round: int
    llm_down: bool
    final: FinalDecision


def default_data_provider(ticker: str) -> dict[str, Any]:
    from tools.market_context import get_global_context, relative_strength

    market = market_data.get_market_snapshot(ticker)
    ctx = get_global_context()
    if market["indicators"].get("status") == "ok":
        market["indicators"].update(relative_strength(market["indicators"], ctx))
    return {
        "ticker": ticker,
        "context": ctx,
        "market": market,
        "fundamentals": fundamental_data.get_fundamentals(ticker),
        "news": news_sentiment.get_company_news(ticker),
        "macro": news_sentiment.get_macro_news(),
    }


def build_graph(llm: LLM, data_provider: Callable[[str], dict] = default_data_provider,
                progress: ProgressFn | None = None):
    say = progress or (lambda msg: log.info(msg))

    def fetch_data(state: AnalysisState) -> AnalysisState:
        say("📥 Veriler çekiliyor (fiyat, teknik, temel, haber)...")
        bundle = data_provider(state["ticker"])
        return {"bundle": bundle, "reports": {}, "checks": {}, "attempts": {}, "feedback": {},
                "pending": list(analysts_mod.ANALYSTS), "debate": [], "round": 0, "llm_down": False}

    def run_analysts(state: AnalysisState) -> AnalysisState:
        pending = state["pending"]
        retry = any(state["attempts"].get(n, 0) for n in pending)
        say(("🔁 Doğrulanamayan analistler yeniden çalışıyor: " if retry else "🧠 4 analist paralel çalışıyor: ")
            + ", ".join(pending))
        new = analysts_mod.run_analysts_parallel(llm, pending, state["ticker"], state["bundle"], state["feedback"])
        attempts = dict(state["attempts"])
        for n in pending:
            attempts[n] = attempts.get(n, 0) + 1
        return {"reports": {**state["reports"], **new}, "attempts": attempts}

    def fact_check(state: AnalysisState) -> AnalysisState:
        say("🔎 1. Kademe: sayısal iddialar ham veriyle doğrulanıyor...")
        checks, feedback, still = dict(state["checks"]), {}, []
        for n in state["pending"]:
            raw = analysts_mod.raw_data_for(n, state["bundle"])
            res = fact_checker.check_report(state["reports"][n], raw, state["ticker"], state["attempts"][n])
            checks[n] = res
            if not res.passed and state["attempts"][n] <= settings.max_fact_check_retries:
                feedback[n] = res.errors
                still.append(n)
        all_failed = all(any(g.startswith("analist_calismadi") for g in r.data_gaps) for r in state["reports"].values())
        if all_failed:
            say("⚠️ Hiçbir analist çalışamadı (LLM erişimi yok) — güvenli varsayılan: BEKLE")
            return {"checks": checks, "pending": [], "feedback": {}, "llm_down": True}
        return {"checks": checks, "pending": still, "feedback": feedback}

    def after_fact_check(state: AnalysisState) -> str:
        if state.get("llm_down"):
            return "risk"
        return "analysts" if state["pending"] else "bull"

    def _turn(side: str, state: AnalysisState) -> AnalysisState:
        rnd = state["round"] + (1 if side == "boga" else 0)
        if side == "boga":
            say(f"🐂🐻 2. Kademe: Boğa/Ayı tartışması — tur {rnd}...")
        try:
            v = debate_mod.run_turn(llm, side, rnd, state["ticker"], state["bundle"], state["reports"],
                                    state["checks"], state["debate"])
        except LLMError as exc:
            log.error("Tartışma turu başarısız (%s): %s", side, exc)
            say(f"⚠️ {side} ajanı LLM'e erişemedi, tartışma kesildi")
            return {"round": rnd, "llm_down": True}
        return {"debate": state["debate"] + [v], "round": rnd}

    def bull(state: AnalysisState) -> AnalysisState:
        return _turn("boga", state)

    def bear(state: AnalysisState) -> AnalysisState:
        return _turn("ayi", state)

    def after_bull(state: AnalysisState) -> str:
        return "risk" if state.get("llm_down") else "bear"

    def after_bear(state: AnalysisState) -> str:
        if state.get("llm_down"):
            return "risk"
        r = state["round"]
        if r < settings.min_debate_rounds:
            return "bull"
        if r < settings.max_debate_rounds and debate_mod.needs_extra_round(state["debate"]):
            return "bull"
        return "risk"

    def risk(state: AnalysisState) -> AnalysisState:
        say("⚖️ 3. Kademe: Risk Yöneticisi karar veriyor...")
        args = (state["ticker"], state["bundle"], state["reports"], state["checks"], state["debate"])
        if state.get("llm_down"):
            return {"final": risk_manager.fallback_decision(*args, reason="LLM erişimi kesildi")}
        try:
            return {"final": risk_manager.decide(llm, *args)}
        except LLMError as exc:
            log.error("Risk Yöneticisi başarısız: %s", exc)
            return {"final": risk_manager.fallback_decision(*args, reason=f"Risk Yöneticisi LLM hatası: {exc}")}

    g = StateGraph(AnalysisState)
    g.add_node("fetch_data", fetch_data)
    g.add_node("analysts", run_analysts)
    g.add_node("fact_check", fact_check)
    g.add_node("bull", bull)
    g.add_node("bear", bear)
    g.add_node("risk", risk)
    g.add_edge(START, "fetch_data")
    g.add_edge("fetch_data", "analysts")
    g.add_edge("analysts", "fact_check")
    g.add_conditional_edges("fact_check", after_fact_check, {"analysts": "analysts", "bull": "bull", "risk": "risk"})
    g.add_conditional_edges("bull", after_bull, {"bear": "bear", "risk": "risk"})
    g.add_conditional_edges("bear", after_bear, {"bull": "bull", "risk": "risk"})
    g.add_edge("risk", END)
    return g.compile()


def run_analysis(ticker: str, llm: LLM, data_provider: Callable[[str], dict] = default_data_provider,
                 progress: ProgressFn | None = None) -> AnalysisState:
    ticker = market_data.normalize_ticker(ticker)
    graph = build_graph(llm, data_provider, progress)
    # recursion_limit: döngüler zaten sınırlı; ek emniyet kemeri
    return graph.invoke({"ticker": ticker}, config={"recursion_limit": 40})
