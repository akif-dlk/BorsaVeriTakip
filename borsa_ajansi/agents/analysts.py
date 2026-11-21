"""Katman 1: Teknik, Temel, Gündem, Duygu analistleri (paralel çalışır, hızlı model)."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agents.llm import LLM, LLMError
from config import prompts
from config.settings import VERI_YOK
from verification.schemas import AnalystName, AnalystReport

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


def raw_data_for(analyst: AnalystName, bundle: dict[str, Any]) -> dict[str, Any]:
    """Analistin gördüğü ham veri. Fact-checker da AYNI sözlükle karşılaştırır."""
    ind = bundle.get("market", {}).get("indicators", {"status": VERI_YOK})
    date = ind.get("date", VERI_YOK)
    news = bundle.get("news", {"status": VERI_YOK})
    if analyst == "teknik":
        return dict(ind)
    if analyst == "temel":
        return {"date": date, "close": ind.get("close", VERI_YOK), **bundle.get("fundamentals", {"status": VERI_YOK})}
    if analyst == "gundem":
        macro = bundle.get("macro", {}).get("macro", VERI_YOK)
        return {
            "status": "ok" if news.get("status") == "ok" or bundle.get("macro", {}).get("status") == "ok" else VERI_YOK,
            "date": date,
            "news_count": news.get("news_count", VERI_YOK) if news.get("status") == "ok" else VERI_YOK,
            "company_news": _titles(news.get("company_news", VERI_YOK)),
            "kap_news": _titles(news.get("kap_news", VERI_YOK)),
            "macro": {k: _titles(v, 4) for k, v in macro.items()} if isinstance(macro, dict) else VERI_YOK,
        }
    if analyst == "duygu":
        sm = bundle.get("market", {}).get("sentiment_metrics", {"status": VERI_YOK})
        return {**sm, "date": date, "news_count": news.get("news_count", VERI_YOK) if news.get("status") == "ok" else VERI_YOK}
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


def run_analysts_parallel(llm: LLM, analysts: list[AnalystName], ticker: str, bundle: dict[str, Any],
                          feedback: dict[str, list[str]] | None = None) -> dict[str, AnalystReport]:
    feedback = feedback or {}

    def _one(name: AnalystName) -> tuple[str, AnalystReport]:
        try:
            return name, run_analyst(llm, name, ticker, raw_data_for(name, bundle), feedback.get(name))
        except LLMError as exc:
            log.error("%s analisti başarısız: %s", name, exc)
            return name, failed_report(name, ticker, str(exc))

    with ThreadPoolExecutor(max_workers=max(1, len(analysts))) as pool:
        return dict(pool.map(_one, analysts))
