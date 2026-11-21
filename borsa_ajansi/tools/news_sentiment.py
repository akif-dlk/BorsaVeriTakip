"""Gündem: şirket/KAP haberleri ve makro (TCMB/FED) başlıkları — RSS üzerinden.

KAP'ın herkese açık resmi API'si olmadığı için KAP bildirimleri Google News RSS'te
'KAP <hisse>' sorgusuyla aranır. Kaynak yanıt vermezse 'veri_yok' döner, uydurma yapılmaz.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote_plus

from config.settings import VERI_YOK

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
MACRO_QUERIES = ["TCMB faiz kararı", "FED faiz kararı", "Borsa İstanbul BIST 100"]


def _parse_feed(url: str) -> list[dict[str, Any]] | None:
    """None = kaynak hatası, [] = sonuç yok."""
    import feedparser

    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 borsa-ajansi"})
    except Exception as exc:
        log.warning("RSS hatası %s: %s", url, exc)
        return None
    if getattr(feed, "bozo", False) and not feed.entries:
        return None
    items = []
    for e in feed.entries:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        items.append({
            "title": e.get("title", ""),
            "link": e.get("link", ""),
            "published": published or VERI_YOK,
            "source": (e.get("source") or {}).get("title", VERI_YOK) if isinstance(e.get("source"), dict) else VERI_YOK,
        })
    return items


def fetch_news(query: str, limit: int = 10, parser: Callable[[str], Any] = _parse_feed) -> list[dict] | None:
    items = parser(GOOGLE_NEWS_RSS.format(q=quote_plus(query)))
    if items is None:
        return None
    return items[:limit]


def _base_symbol(ticker: str) -> str:
    return ticker.split(".")[0].upper()


def get_company_news(ticker: str, limit: int = 10, parser: Callable[[str], Any] = _parse_feed) -> dict[str, Any]:
    sym = _base_symbol(ticker)
    company = fetch_news(f"{sym} hisse", limit, parser)
    kap = fetch_news(f"KAP {sym}", limit, parser)
    status = "ok" if (company is not None or kap is not None) else VERI_YOK
    return {
        "status": status,
        "ticker": ticker,
        "company_news": company if company is not None else VERI_YOK,
        "kap_news": kap if kap is not None else VERI_YOK,
        "news_count": len(company or []) + len(kap or []),
    }


def get_macro_news(limit: int = 5, parser: Callable[[str], Any] = _parse_feed) -> dict[str, Any]:
    out: dict[str, Any] = {}
    any_ok = False
    for q in MACRO_QUERIES:
        items = fetch_news(q, limit, parser)
        out[q] = items if items is not None else VERI_YOK
        any_ok = any_ok or items is not None
    return {"status": "ok" if any_ok else VERI_YOK, "macro": out}
