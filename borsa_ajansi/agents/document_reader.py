"""Belge okuyucu: KAP bildirim metinleri + faaliyet raporu / yatırımcı sunumu -> doğrulanmış yapılandırılmış özet.

LLM (hızlı model) yalnızca okur ve özetler. Halüsinasyona karşı PROGRAMATİK doğrulama:
- Her maddenin `evidence_quote` alanı kaynak metinde birebir (normalize edilmiş) geçmeli.
- Madde metnindeki her sayı kaynak metinde geçmeli.
Geçemeyen maddeler atılır ve sayısı raporlanır. Sonuçlar SQLite'ta önbelleklenir (aynı belge için tekrar LLM yok).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from agents.llm import LLM, LLMError
from config import prompts
from config.settings import VERI_YOK, settings
from memory.cache import DocCache
from tools import kap
from verification.schemas import DisclosureDigest, OutlookDigest

log = logging.getLogger(__name__)

KAP_LOOKBACK_DAYS = 45
KAP_MAX_DISCLOSURES = 6
OUTLOOK_LOOKBACK_DAYS = 200
OUTLOOK_MAX_DETAIL_FETCH = 4
OUTLOOK_TEXT_BUDGET = 12000

_SKIP_TITLES = re.compile(r"sorumluluk beyan|genel bilgi formu|^finansal rapor$|faaliyet raporu", re.I)
_OUTLOOK_DISC = re.compile(r"faaliyet raporu|finansal sonu|sunum|yatırımcı|investor|strateji|hedef|beklenti|analist", re.I)
_PRES_NAME = re.compile(r"sunum|presentation|investor|yatırımcı|yatirimci|beklenti|guidance", re.I)
_ACT_NAME = re.compile(r"faaliyet|activity", re.I)
_EXCL_NAME = re.compile(r"ifrs|tfrs|tms|denetim|trafik|traffic|finansal tablo|financial statement|dipnot", re.I)


# ---------------------------------------------------------------------------
# Programatik doğrulama
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = s.replace("İ", "i").replace("I", "ı").lower()
    s = re.sub(r"[^\w]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_ok(quote: str, source: str) -> bool:
    q, src = _norm(quote), _norm(source)
    if len(q) < 15:
        return False
    if q in src:
        return True
    # Uzun alıntılarda baş ve son kısım ayrı ayrı geçmeli (PDF satır kırılmalarına tolerans)
    return len(q) > 80 and q[:60] in src and q[-60:] in src


_NUM = re.compile(r"\d[\d.,]*")


def _nums(s: str) -> set[str]:
    return {re.sub(r"\D", "", m).lstrip("0") or "0" for m in _NUM.findall(s)}


def numbers_ok(text: str, source: str) -> bool:
    return _nums(text) <= _nums(source)


# ---------------------------------------------------------------------------
def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _recent(disclosures: list[dict], days: int, today: datetime) -> list[dict]:
    lim = today - timedelta(days=days)
    return [d for d in disclosures if (dt := _parse_date(d.get("date", ""))) and dt >= lim]


def build_kap_digest(llm: LLM, symbol: str, disclosures: list[dict] | None, get, cache: DocCache | None,
                     today: datetime) -> dict[str, Any]:
    if disclosures is None:
        return {"status": VERI_YOK, "reason": "KAP'a erişilemedi"}
    recent = [d for d in _recent(disclosures, KAP_LOOKBACK_DAYS, today)
              if d.get("class") != "DG" and not _SKIP_TITLES.search(d.get("title", ""))][:KAP_MAX_DISCLOSURES]
    if not recent:
        return {"status": "ok", "disclosure_count": 0, "notes": [], "note": f"son {KAP_LOOKBACK_DAYS} günde önemli bildirim yok"}

    key = f"kapdigest:{symbol}:" + ",".join(str(d["index"]) for d in recent)
    if cache and (hit := cache.get(key)):
        return hit

    texts: dict[int, str] = {}
    for d in recent:
        det = kap.disclosure_detail(d["index"], get)
        texts[d["index"]] = (det or {}).get("text") or f"{d['title']} — {d['summary']}"
    payload = [{"disclosure_index": d["index"], "date": d["date"], "title": d["title"], "summary": d["summary"],
                "text": texts[d["index"]]} for d in recent]
    basic = [{"date": d["date"], "title": d["title"], "summary": d["summary"]} for d in recent]
    try:
        digest = llm.with_structured_output(DisclosureDigest, tier="fast").invoke(
            prompts.KAP_READER, f"Hisse: {symbol}\nBildirimler (JSON):\n{json.dumps(payload, ensure_ascii=False)}")
    except LLMError as exc:
        log.warning("KAP özeti üretilemedi (%s): %s", symbol, exc)
        return {"status": "ok", "disclosure_count": len(recent), "notes": [], "headlines": basic,
                "llm_summary": VERI_YOK}

    by_idx = {d["index"]: d for d in recent}
    notes, rejected = [], 0
    for n in digest.notes:
        src = texts.get(n.disclosure_index)
        if src is None or not quote_ok(n.evidence_quote, src) or not numbers_ok(n.summary, src):
            rejected += 1
            continue
        d = by_idx[n.disclosure_index]
        notes.append({"date": d["date"], "title": d["summary"] or d["title"], "impact": n.impact,
                      "materiality": n.materiality, "summary": n.summary})
    out = {"status": "ok", "source": "KAP", "disclosure_count": len(recent), "notes": notes,
           "rejected_notes": rejected}
    if cache:
        cache.set(key, out)
    return out


def find_outlook_documents(disclosures: list[dict], get, today: datetime) -> list[dict]:
    """Son faaliyet raporu ve yatırımcı sunumunu (PDF) bulur; finansal tablo / trafik eklerini eler."""
    cands = [d for d in _recent(disclosures, OUTLOOK_LOOKBACK_DAYS, today)
             if d.get("attachments") and _OUTLOOK_DISC.search(f"{d.get('title', '')} {d.get('summary', '')}")]
    chosen: dict[str, dict] = {}
    for d in cands[:OUTLOOK_MAX_DETAIL_FETCH]:
        det = kap.disclosure_detail(d["index"], get) or {}
        atts = sorted(det.get("attachments", []), key=lambda a: (" EN" in a["name"] or "_EN" in a["name"], a["name"]))
        for a in atts:
            name = a["name"]
            if a["ext"] != "pdf" or _EXCL_NAME.search(name):
                continue
            kind = "sunum" if _PRES_NAME.search(name) else "faaliyet" if _ACT_NAME.search(name) else None
            if kind and kind not in chosen:
                chosen[kind] = {"id": a["id"], "name": name, "kind": kind, "date": d["date"]}
        if len(chosen) == 2:
            break
    return list(chosen.values())


def build_outlook(llm: LLM, symbol: str, disclosures: list[dict] | None, get, cache: DocCache | None,
                  today: datetime) -> dict[str, Any]:
    if disclosures is None:
        return {"status": VERI_YOK, "reason": "KAP'a erişilemedi"}
    docs = find_outlook_documents(disclosures, get, today)
    if not docs:
        return {"status": VERI_YOK, "reason": f"son {OUTLOOK_LOOKBACK_DAYS} günde faaliyet raporu / sunum bulunamadı"}

    key = f"outlook:{symbol}:" + ",".join(d["id"] for d in docs)
    if cache and (hit := cache.get(key)):
        return hit

    texts: dict[str, str] = {}
    for d in docs:
        data = kap.download_attachment(d["id"], get)
        sel = kap.select_outlook_text(kap.pdf_pages(data), OUTLOOK_TEXT_BUDGET) if data else ""
        if sel:
            texts[d["name"]] = sel
    if not texts:
        return {"status": VERI_YOK, "reason": "PDF metni çıkarılamadı"}

    user = f"Hisse: {symbol}\n" + "\n\n".join(f"=== Belge: {n} ===\n{t}" for n, t in texts.items())
    try:
        dg = llm.with_structured_output(OutlookDigest, tier="fast").invoke(prompts.OUTLOOK_READER, user)
    except LLMError as exc:
        log.warning("Rapor özeti üretilemedi (%s): %s", symbol, exc)
        return {"status": VERI_YOK, "reason": f"LLM hatası: {exc}"[:200],
                "documents": [{"name": d["name"], "date": d["date"]} for d in docs]}

    all_text = "\n".join(texts.values())
    items, rejected = [], 0
    for it in dg.items[:10]:
        src = texts.get(it.source, all_text)
        if not quote_ok(it.evidence_quote, src) or not numbers_ok(f"{it.statement} {it.horizon}", src):
            rejected += 1
            continue
        items.append({"category": it.category, "statement": it.statement, "horizon": it.horizon, "source": it.source})
    summary = dg.summary if numbers_ok(dg.summary, all_text) else VERI_YOK
    out = {"status": "ok" if items else VERI_YOK, "documents": [{"name": d["name"], "date": d["date"]} for d in docs
                                                              if d["name"] in texts],
           "summary": summary, "items": items, "rejected_items": rejected}
    if cache and items:
        cache.set(key, out)
    return out


def get_documents(llm: LLM, ticker: str, get: Callable = kap.http_get, cache: DocCache | None = None,
                  today: datetime | None = None) -> dict[str, Any]:
    symbol = kap.base_symbol(ticker)
    today = today or datetime.now()
    if cache is None:
        try:
            cache = DocCache(settings.db_path)
        except Exception as exc:
            log.warning("Belge önbelleği açılamadı: %s", exc)
    try:
        disclosures = kap.list_disclosures(symbol, get)
    except Exception as exc:
        log.warning("KAP listesi alınamadı %s: %s", symbol, exc)
        disclosures = None
    out: dict[str, Any] = {}
    for name, fn in (("kap", build_kap_digest), ("outlook", build_outlook)):
        try:
            out[name] = fn(llm, symbol, disclosures, get, cache, today)
        except Exception as exc:  # belge katmanı asla analizi düşürmez
            log.exception("Belge katmanı hatası (%s/%s)", symbol, name)
            out[name] = {"status": VERI_YOK, "reason": f"hata: {exc}"[:200]}
    return out
