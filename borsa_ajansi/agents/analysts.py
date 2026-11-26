"""Katman 1: Teknik, Temel, Gündem, Duygu analistleri (paralel çalışır, hızlı model)."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents.llm import LLM, LLMError
from config import prompts
from config.settings import VERI_YOK, settings
from verification.schemas import AnalystName, AnalystReport, BatchAnalystReports

log = logging.getLogger(__name__)

ANALYSTS: tuple[AnalystName, ...] = ("teknik", "temel", "gundem", "duygu")

SYSTEM_PROMPTS: dict[str, str] = {
    "teknik": prompts.TECHNICAL_ANALYST,
    "temel": prompts.FUNDAMENTAL_ANALYST,
    "gundem": prompts.NEWS_ANALYST,
    "duygu": prompts.SENTIMENT_ANALYST,
}


def _titles(items: Any, limit: int = 8) -> Any:
    if not isinstance(items, list):
        return items  # 'veri_yok'
    return [{"title": i.get("title"), "published": i.get("published")} for i in items[:limit]]


def _outlook_view(o: dict | None) -> Any:
    if not o or o.get("status") != "ok":
        return {"status": VERI_YOK, "reason": (o or {}).get("reason", "belge okunmadı")}
    return {"status": "ok", "documents": [d["name"] for d in o.get("documents", [])], "summary": o.get("summary"),
            "items": [{k: i[k] for k in ("category", "statement", "horizon")} for i in o.get("items", [])]}


def _kap_view(k: dict | None) -> Any:
    if not k or k.get("status") != "ok":
        return {"status": VERI_YOK, "reason": (k or {}).get("reason", "KAP okunmadı")}
    view: dict[str, Any] = {"status": "ok", "notes": k.get("notes", [])}
    if k.get("headlines"):
        view["headlines_only"] = k["headlines"]
    return view


def raw_data_for(analyst: AnalystName, bundle: dict[str, Any]) -> dict[str, Any]:
    """Analistin gördüğü ham veri. Fact-checker da AYNI sözlükle karşılaştırır."""
    ind = bundle.get("market", {}).get("indicators", {"status": VERI_YOK})
    date = ind.get("date", VERI_YOK)
    news = bundle.get("news", {"status": VERI_YOK})
    if analyst == "teknik":
        return dict(ind)
    docs = bundle.get("documents") or {}
    if analyst == "temel":
        return {"date": date, "close": ind.get("close", VERI_YOK), **bundle.get("fundamentals", {"status": VERI_YOK}),
                "financial_trend": bundle.get("financials", {"status": VERI_YOK}),
                "company_outlook": _outlook_view(docs.get("outlook"))}
    if analyst == "gundem":
        macro = bundle.get("macro", {}).get("macro", VERI_YOK)
        return {
            "status": "ok" if news.get("status") == "ok" or bundle.get("macro", {}).get("status") == "ok" else VERI_YOK,
            "date": date,
            "news_count": news.get("news_count", VERI_YOK) if news.get("status") == "ok" else VERI_YOK,
            "company_news": _titles(news.get("company_news", VERI_YOK)),
            "kap_news": _titles(news.get("kap_news", VERI_YOK)),
            "macro": {k: _titles(v, 4) for k, v in macro.items()} if isinstance(macro, dict) else VERI_YOK,
            "market_context": bundle.get("context", VERI_YOK),
            "kap_disclosures": _kap_view(docs.get("kap")),
        }
    if analyst == "duygu":
        sm = bundle.get("market", {}).get("sentiment_metrics", {"status": VERI_YOK})
        ctx = bundle.get("context", {}) or {}
        return {
            **sm, "date": date,
            "news_count": news.get("news_count", VERI_YOK) if news.get("status") == "ok" else VERI_YOK,
            "vix": ctx.get("vix", {}).get("close", VERI_YOK),
            "global_risk_regime": ctx.get("global_risk_regime", VERI_YOK),
            "bist100_change_5d_pct": ctx.get("bist100", {}).get("change_5d_pct", VERI_YOK),
            "usdtry_change_5d_pct": ctx.get("usdtry", {}).get("change_5d_pct", VERI_YOK),
            "rel_strength_5d_pct": ind.get("rel_strength_5d_pct", VERI_YOK),
            "rel_strength_20d_pct": ind.get("rel_strength_20d_pct", VERI_YOK),
        }
    raise ValueError(analyst)


def build_user_prompt(analyst: AnalystName, ticker: str, raw: dict[str, Any], feedback: list[str] | None) -> str:
    msg = f"Hisse: {ticker}\nAnalist: {analyst}\nHam veri (JSON):\n{json.dumps(raw, ensure_ascii=False, default=str)}"
    if feedback:
        msg += "\n\n" + prompts.FACT_CHECK_FEEDBACK.format(errors="\n".join(f"- {e}" for e in feedback))
    return msg


def run_analyst(llm: LLM, analyst: AnalystName, ticker: str, raw: dict[str, Any],
                feedback: list[str] | None = None) -> AnalystReport:
    runnable = llm.with_structured_output(AnalystReport, tier="fast")
    report = runnable.invoke(SYSTEM_PROMPTS[analyst], build_user_prompt(analyst, ticker, raw, feedback))
    # Kimlik alanlarını sabitle (LLM yanlış yazsa bile sözleşme bozulmasın)
    return report.model_copy(update={"analyst": analyst, "ticker": ticker})


def failed_report(analyst: AnalystName, ticker: str, error: str) -> AnalystReport:
    """LLM çağrısı tamamen başarısızsa uydurma yerine açıkça 'veri_yok' rapor."""
    return AnalystReport(
        analyst=analyst, ticker=ticker, data_date=VERI_YOK, signal="notr", score=0.0, confidence=0.0,
        key_points=[], claims=[], data_gaps=[f"analist_calismadi: {error}"], summary=VERI_YOK,
    )


def run_analysts_batch(llm: LLM, ticker: str, bundle: dict[str, Any]) -> dict[str, AnalystReport]:
    """Ekonomik mod: 4 analist raporunu 4 ayrı istek yerine TEK bir yapılandırılmış LLM çağrısında üretir."""
    raw_all = {name: raw_data_for(name, bundle) for name in ANALYSTS}
    system = (
        prompts._DESK
        + "\nAşağıda hissenin 4 ayrı uzmanlık alanı (`teknik`, `temel`, `gundem`, `duygu`) için hazırlanmış ham JSON "
        "verileri yer alıyor. Her analist alanı için YALNIZCA kendi ham veri sözlüğündeki alan adlarını (`claims.field`) "
        "ve değerlerini AYNEN kopyalayarak bağımsız birer `AnalystReport` üret.\n"
        + prompts._COMMON_RULES
    )
    user = f"Hisse: {ticker}\n4 Analist Ham Verileri (JSON):\n{json.dumps(raw_all, ensure_ascii=False, default=str)}"
    batch = llm.with_structured_output(BatchAnalystReports, tier="fast").invoke(system, user)
    return {
        name: getattr(batch, name).model_copy(update={"analyst": name, "ticker": ticker})
        for name in ANALYSTS
    }


def run_analysts_parallel(llm: LLM, analysts: list[AnalystName], ticker: str, bundle: dict[str, Any],
                          feedback: dict[str, list[str]] | None = None) -> dict[str, AnalystReport]:
    feedback = feedback or {}

    if settings.economy_single_call and not feedback and set(analysts) == set(ANALYSTS):
        try:
            return run_analysts_batch(llm, ticker, bundle)
        except Exception as exc:
            log.warning("Tek-çağrı analist modu başarısız, paralel moda düşülüyor: %s", exc)

    def _one(name: AnalystName) -> tuple[str, AnalystReport]:
        try:
            return name, run_analyst(llm, name, ticker, raw_data_for(name, bundle), feedback.get(name))
        except LLMError as exc:
            log.error("%s analisti başarısız: %s", name, exc)
            return name, failed_report(name, ticker, str(exc))

    with ThreadPoolExecutor(max_workers=max(1, len(analysts))) as pool:
        return dict(pool.map(_one, analysts))
