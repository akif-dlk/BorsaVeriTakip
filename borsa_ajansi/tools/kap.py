"""KAP (Kamuyu Aydınlatma Platformu): şirketin kendi bildirimleri, bildirim metni ve PDF ekleri.

KAP'ın resmi açık API'si yoktur; kap.org.tr'nin herkese açık sayfaları okunur. Sayfa yapısı değişirse ya da
site yanıt vermezse fonksiyonlar boş liste / None döndürür ve üst katman 'veri_yok' işaretler (uydurma yok).
"""
from __future__ import annotations

import html
import io
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

BASE = "https://www.kap.org.tr"
_HEADERS = {"User-Agent": "Mozilla/5.0 (borsa-ajansi; kisisel arastirma)", "Accept-Language": "tr"}
TIMEOUT = 25

HttpGet = Callable[[str], "bytes | None"]


def http_get(url: str) -> bytes | None:
    import requests

    try:
        r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("KAP %s -> HTTP %s", url, r.status_code)
            return None
        return r.content
    except Exception as exc:
        log.warning("KAP erişim hatası %s: %s", url, exc)
        return None


def base_symbol(ticker: str) -> str:
    return ticker.upper().split(".")[0]


def member_info(symbol: str, get: HttpGet = http_get) -> dict[str, str] | None:
    raw = get(f"{BASE}/tr/api/member/filter/{symbol}")
    if not raw:
        return None
    try:
        rows = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    row = next((r for r in rows if r.get("mkkMemberOid")), None)
    return {"oid": row["mkkMemberOid"], "title": row.get("title", "")} if row else None


_DISC_RE = re.compile(r'\{"publishDate":\s*"[^"]*".*?"fundCode":\s*(?:null|"[^"]*")\}')


def parse_disclosure_list(page: str, company_title: str, symbol: str) -> list[dict[str, Any]]:
    """Sorgu sayfasına gömülü JSON'dan şirketin KENDİ bildirimlerini çıkarır (MKK/BIST duyuruları elenir)."""
    text = page.replace('\\"', '"')
    out, seen = [], set()
    for m in _DISC_RE.finditer(text):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        idx = d.get("disclosureIndex")
        own = d.get("companyTitle") == company_title or d.get("stockCode") == symbol
        if not own or idx in seen:
            continue
        seen.add(idx)
        try:
            dt = datetime.strptime(d["publishDate"], "%d.%m.%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            dt = d.get("publishDate", "")
        out.append({"index": int(idx), "date": dt, "title": d.get("title") or "", "summary": d.get("summary") or "",
                    "class": d.get("disclosureClass") or "", "attachments": int(d.get("attachmentCount") or 0)})
    out.sort(key=lambda r: r["index"], reverse=True)
    return out


def list_disclosures(symbol: str, get: HttpGet = http_get) -> list[dict[str, Any]] | None:
    """None = kaynak hatası; [] = bildirim yok."""
    info = member_info(symbol, get)
    if not info:
        return None
    raw = get(f"{BASE}/tr/bildirim-sorgu-sonuc?member={info['oid']}")
    if not raw:
        return None
    return parse_disclosure_list(raw.decode("utf-8", "replace"), info["title"], symbol)


def _strip_html(raw: str) -> str:
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return html.unescape(re.sub(r"\s+", " ", txt)).strip()


_ATT_RE = re.compile(r'"attachments":(\[[^\]]*\])')


def parse_disclosure_page(raw: str, max_chars: int = 3000) -> dict[str, Any]:
    """Bildirim sayfası -> {'text': gövde metni, 'attachments': [{'id','name','ext'}]}."""
    atts: dict[str, dict] = {}
    for m in _ATT_RE.finditer(raw.replace('\\"', '"')):
        try:
            for a in json.loads(m.group(1)):
                if a.get("objId"):
                    atts[a["objId"]] = {"id": a["objId"], "name": a.get("fileName", ""),
                                        "ext": (a.get("fileExtension") or "").lower()}
        except ValueError:
            continue
    text = _strip_html(raw)
    i = text.find("Özet Bilgi")
    body = text[i + len("Özet Bilgi"):] if i >= 0 else text
    body = re.sub(r"\b\w+_\w+\|", " ", body)  # 'oda_ExplanationTextBlock|' gibi etiketler
    body = re.sub(r"\[[A-Z_]+\]", " ", body)
    for cut in ("Bildirim Sorguları", "Copyright ©"):
        j = body.find(cut)
        if j > 200:
            body = body[:j]
    # Form kalıbını at: "<özet> İlgili Şirketler ... Açıklamalar Explanations <asıl metin>"
    k = body.find("Açıklamalar Explanations")
    if k >= 0:
        head = body.split("İlgili Şirketler")[0].strip()
        body = f"{head} — {body[k + len('Açıklamalar Explanations'):]}"
    return {"text": re.sub(r"\s+", " ", body).strip()[:max_chars], "attachments": list(atts.values())}


def disclosure_detail(index: int, get: HttpGet = http_get, max_chars: int = 3000) -> dict[str, Any] | None:
    raw = get(f"{BASE}/tr/Bildirim/{index}")
    if not raw:
        return None
    return parse_disclosure_page(raw.decode("utf-8", "replace"), max_chars)


def download_attachment(obj_id: str, get: HttpGet = http_get) -> bytes | None:
    return get(f"{BASE}/tr/api/file/download/{obj_id}")


# ---------------------------------------------------------------------------
# PDF metni: geleceğe dönük ifadelerin yoğun olduğu sayfalar seçilir (rapor 100+ sayfa olabilir)
# ---------------------------------------------------------------------------
OUTLOOK_KEYWORDS = [
    r"hedefl", r"beklenti", r"öngör", r"stratej", r"yatırım plan", r"yatırım harcama", r"kapasite", r"sipariş",
    r"yeni proje", r"büyüme", r"pazar payı", r"ihracat", r"guidance", r"outlook", r"target", r"expect",
    r"strateg", r"capex", r"pipeline", r"backlog", r"20(?:2[7-9]|3\d)[eft]?",
]
_KW_RE = re.compile("|".join(OUTLOOK_KEYWORDS), re.I)


def pdf_pages(data: bytes, max_pages: int = 250) -> list[str]:
    from pypdf import PdfReader

    k = data.find(b"%PDF")  # KAP bazen dosyanın önüne sarmalayıcı bayt ekler
    if k > 0:
        data = data[k:]
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for p in reader.pages[:max_pages]:
            try:
                pages.append(re.sub(r"\s+", " ", p.extract_text() or "").strip())
            except Exception:
                pages.append("")
        return pages
    except Exception as exc:
        log.warning("PDF okunamadı: %s", exc)
        return []


def select_outlook_text(pages: list[str], budget: int = 14000) -> str:
    """Anahtar kelime yoğunluğuna göre sayfa seçer, sayfa sırasını koruyarak bütçeye sığdırır."""
    scored = []
    for i, t in enumerate(pages):
        score = float(len(_KW_RE.findall(t)))
        digits = sum(ch.isdigit() for ch in t) / max(1, len(t))
        if digits > 0.25:  # grafik/tablo sayfası: metin değil sayı yığını
            score *= 0.3
        if score and len(t) > 200:
            scored.append((score, i))
    chosen, used = [], 0
    for score, i in sorted(scored, reverse=True):
        t = pages[i][:4000]
        if used + len(t) > budget:
            continue
        chosen.append(i)
        used += len(t)
    return "\n".join(f"[sayfa {i + 1}] {pages[i][:4000]}" for i in sorted(chosen))
