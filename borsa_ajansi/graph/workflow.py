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
from agents import document_reader
from tools import financials, fundamental_data, market_data, news_sentiment
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
    debate_failed: bool
    final: FinalDecision


def default_data_provider(ticker: str) -> dict[str, Any]:
    from tools.market_context import get_global_context, relative_strength

    market = market_data.get_market_snapshot(ticker)
    ctx = get_global_context()
    if market["indicators"].get("status") == "ok":
        market["indicators"].update(relative_strength(market["indicators"], ctx))
    fund = fundamental_data.get_fundamentals(ticker)
    cur = fund.get("financial_currency")
    trend = financials.get_financial_trend(ticker, currency=cur if cur != "veri_yok" else None)
    fund = financials.reconcile_fundamentals(fund, trend)
    return {
        "ticker": ticker,
        "context": ctx,
        "market": market,
        "fundamentals": fund,
        "financials": trend,
        "news": news_sentiment.get_company_news(ticker),
        "macro": news_sentiment.get_macro_news(),
    }


DocumentProvider = Callable[[LLM, str], dict]


def build_graph(llm: LLM, data_provider: Callable[[str], dict] = default_data_provider,
                progress: ProgressFn | None = None, document_provider: DocumentProvider | None = None):
    say = progress or (lambda msg: log.info(msg))
    if document_provider is None and settings.documents_enabled:
        document_provider = document_reader.get_documents

    def fetch_data(state: AnalysisState) -> AnalysisState:
        say("📥 Veriler çekiliyor (fiyat, teknik, temel, haber)...")
        bundle = data_provider(state["ticker"])
        if document_provider and "documents" not in bundle:
            say("📄 KAP bildirimleri ve şirket raporları/sunumları okunuyor...")
            try:
                bundle = {**bundle, "documents": document_provider(llm, state["ticker"])}
            except Exception as exc:  # belge katmanı analizi asla düşürmez
                log.exception("Belge okuma hatası")
                bundle = {**bundle, "documents": {"kap": {"status": "veri_yok", "reason": str(exc)[:200]},
                                                  "outlook": {"status": "veri_yok", "reason": str(exc)[:200]}}}
        return {"bundle": bundle, "reports": {}, "checks": {}, "attempts": {}, "feedback": {},
                "pending": list(analysts_mod.ANALYSTS), "debate": [], "round": 0, "llm_down": False,
                "debate_failed": False}

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
            if len(state["debate"]) >= 2:
                # En az bir tam tur (boğa+ayı) var: karar yine verilir, güven cezalı
                say(f"⚠️ {side} ajanı LLM'e erişemedi, tartışma {len(state['debate']) // 2}. turdan sonra kesildi")
                return {"round": rnd, "debate_failed": True}
            say(f"⚠️ {side} ajanı LLM'e erişemedi, tartışma yapılamadı")
            return {"round": rnd, "llm_down": True}
        return {"debate": state["debate"] + [v], "round": rnd}

    def bull(state: AnalysisState) -> AnalysisState:
        return _turn("boga", state)

    def bear(state: AnalysisState) -> AnalysisState:
        return _turn("ayi", state)

    def after_bull(state: AnalysisState) -> str:
        return "risk" if state.get("llm_down") or state.get("debate_failed") else "bear"

    def after_bear(state: AnalysisState) -> str:
        if state.get("llm_down") or state.get("debate_failed"):
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
            extra = (0.10, "tartışma LLM hatasıyla erken kesildi") if state.get("debate_failed") else None
            calib = None
            if data_provider is default_data_provider:
                try:
                    from memory.db import PredictionStore

                    calib = PredictionStore().calibration_summary()
                except Exception:
                    calib = None
            return {"final": risk_manager.decide(llm, *args, extra=extra, calibration=calib)}
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


def _serialize_state(state: AnalysisState) -> dict[str, Any]:
    return {
        "ticker": state["ticker"],
        "bundle": state.get("bundle", {}),
        "reports": {k: v.model_dump() for k, v in state.get("reports", {}).items()},
        "checks": {k: v.model_dump() for k, v in state.get("checks", {}).items()},
        "attempts": state.get("attempts", {}),
        "debate": [v.model_dump() for v in state.get("debate", [])],
        "round": state.get("round", 0),
        "llm_down": state.get("llm_down", False),
        "debate_failed": state.get("debate_failed", False),
        "final": state["final"].model_dump(),
    }


def _deserialize_state(raw: dict[str, Any]) -> AnalysisState:
    return {
        "ticker": raw["ticker"],
        "bundle": raw.get("bundle", {}),
        "reports": {k: AnalystReport.model_validate(v) for k, v in raw.get("reports", {}).items()},
        "checks": {k: FactCheckResult.model_validate(v) for k, v in raw.get("checks", {}).items()},
        "attempts": raw.get("attempts", {}),
        "debate": [ValidatedTurn.model_validate(v) for v in raw.get("debate", [])],
        "round": raw.get("round", 0),
        "llm_down": raw.get("llm_down", False),
        "debate_failed": raw.get("debate_failed", False),
        "final": FinalDecision.model_validate(raw["final"]),
    }


def run_analysis(ticker: str, llm: LLM, data_provider: Callable[[str], dict] = default_data_provider,
                 progress: ProgressFn | None = None,
                 document_provider: DocumentProvider | None = None) -> AnalysisState:
    ticker = market_data.normalize_ticker(ticker)
    use_cache = settings.analysis_cache_ttl_s > 0 and data_provider is default_data_provider
    cache = None
    cache_key = f"analysis:v1:{ticker}"
    if use_cache:
        from memory.cache import DocCache

        try:
            cache = DocCache(settings.db_path)
            if hit := cache.get(cache_key, max_age_s=settings.analysis_cache_ttl_s):
                if progress:
                    progress(f"⚡ {ticker} son analiz önbellekten yüklendi (0 LLM çağrısı)")
                return _deserialize_state(hit)
        except Exception as exc:
            log.debug("Analiz önbelleği okunamadı: %s", exc)

    graph = build_graph(llm, data_provider, progress, document_provider)
    # recursion_limit: döngüler zaten sınırlı; ek emniyet kemeri
    state = graph.invoke({"ticker": ticker}, config={"recursion_limit": 40})

    if cache is not None and not state.get("llm_down") and "final" in state:
        try:
            cache.set(cache_key, _serialize_state(state))
        except Exception as exc:
            log.debug("Analiz önbelleği yazılamadı: %s", exc)
    return state
