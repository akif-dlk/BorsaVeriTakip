"""Çeyreklik trend, KAP ayrıştırma ve belge okuyucunun programatik doğrulaması (ağ ve gerçek LLM yok)."""
import json
from datetime import datetime

import pandas as pd
import pytest

from agents import analysts, document_reader as dr
from agents.llm import LLMError
from graph.report import format_report
from graph.workflow import run_analysis
from memory.cache import DocCache
from tests.fakes import FakeLLM, make_bundle
from tests.test_workflow import Script
from tools import financials, kap
from verification.schemas import DisclosureDigest, DisclosureNote, OutlookDigest, OutlookItem

Q = pd.to_datetime(["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])


def _income():
    return pd.DataFrame({
        Q[0]: [120.0, 30.0, 12.0], Q[1]: [100.0, 25.0, 10.0], Q[2]: [90.0, 20.0, -5.0],
        Q[3]: [95.0, 22.0, 8.0], Q[4]: [100.0, 24.0, 10.0],
    }, index=["Total Revenue", "EBITDA", "Net Income"])


def _balance():
    return pd.DataFrame({Q[0]: [50.0, 10.0, 200.0, 40.0], Q[4]: [60.0, 10.0, 160.0, 50.0]},
                        index=["Total Debt", "Cash And Cash Equivalents", "Stockholders Equity", "Net Debt"])


# ---------------- çeyreklik trend ----------------
def test_financial_trend_math():
    t = financials.compute_financial_trend(_income(), _balance(), "USD")
    assert t["status"] == "ok" and t["latest_quarter"] == "2026-09-30"
    assert t["revenue_yoy_pct"] == 20.0 and t["revenue_qoq_pct"] == 20.0
    assert t["net_income_yoy_pct"] == 20.0
    assert t["net_margin_pct"] == 10.0 and t["net_margin_year_ago_pct"] == 10.0 and t["net_margin_change_pts"] == 0.0
    assert t["ebitda_margin_pct"] == 25.0 and t["ebitda_margin_change_pts"] == 1.0
    assert t["ttm_revenue"] == 405.0 and t["ttm_net_income"] == 25.0 and t["profitable_quarters_last4"] == 3
    b = t["balance"]
    assert b["net_debt"] == 40.0 and b["net_debt_yoy_pct"] == -20.0 and b["debt_to_equity"] == 0.25
    assert b["equity_yoy_pct"] == 25.0
    assert t["reporting_currency"] == "USD"


def test_reconcile_prefers_ttm_ebitda():
    t = financials.compute_financial_trend(_income(), _balance(), "USD")
    assert t["ttm_ebitda"] == 97.0
    fund = {"status": "ok", "ebitda": 30.0, "net_debt": 194.0, "net_debt_to_ebitda": 6.47, "suspicious_values_removed": []}
    r = financials.reconcile_fundamentals(fund, t)
    assert r["ebitda"] == 97.0 and r["net_debt_to_ebitda"] == 2.0 and r["ebitda_basis"] == "son_4_ceyrek_ttm"
    assert "TTM kullanıldı" in r["suspicious_values_removed"][0]
    assert financials.reconcile_fundamentals(fund, {"status": "veri_yok"}) is fund


def test_financial_trend_missing_data_is_veri_yok():
    assert financials.compute_financial_trend(None, None)["status"] == "veri_yok"
    inc = _income().drop(columns=[Q[4]])  # geçen yılın aynı çeyreği yok
    t = financials.compute_financial_trend(inc, None)
    assert t["revenue_yoy_pct"] == "veri_yok" and t["balance"] == {"status": "veri_yok"}
    assert "revenue_yoy_pct" in t["missing_fields"]
    assert financials.get_financial_trend("X", fetcher=lambda t: {})["status"] == "veri_yok"


# ---------------- KAP ayrıştırma ----------------
def _disc(idx, date, title, summary, company="ACME A.Ş.", cls="ODA", att=0):
    return {"publishDate": date, "disclosureIndex": idx, "stockCode": None, "companyTitle": company, "title": title,
            "summary": summary, "disclosureClass": cls, "attachmentCount": att, "fundCode": None}


def _list_page(items):
    # KAP sayfası JSON'u RSC string'i içinde kaçışlı tutar
    return "<html>" + json.dumps({"data": [{"disclosureBasic": d} for d in items]}).replace('"', '\\"') + "</html>"


def test_parse_disclosure_list_keeps_only_own_company():
    page = _list_page([
        _disc(3, "20.11.2026 10:00:00", "Özel Durum Açıklaması (Genel)", "Yeni sözleşme"),
        _disc(2, "19.11.2026 23:00:00", "SPK İşlem Yasağı", "x", company="MERKEZİ KAYIT KURULUŞU A.Ş."),
        _disc(1, "01.11.2026 08:00:00", "Faaliyet Raporu (Konsolide)", "9A", cls="FR", att=1),
    ])
    rows = kap.parse_disclosure_list(page, "ACME A.Ş.", "ACME")
    assert [r["index"] for r in rows] == [3, 1]
    assert rows[0]["date"] == "2026-11-20 10:00" and rows[1]["attachments"] == 1


def test_parse_disclosure_page_text_and_attachments():
    raw = ('<html><body>menü Özet Bilgi Yeni sözleşme İlgili Şirketler [] oda_X| Açıklamalar Explanations '
           'Şirketimiz 250 milyon TL tutarında ihale kazanmıştır.</body>'
           '<script>self.__next_f.push("\\"attachments\\":[{\\"objId\\":\\"abc\\",\\"fileName\\":\\"Sunum.pdf\\",'
           '\\"fileExtension\\":\\"pdf\\"}]")</script></html>')
    p = kap.parse_disclosure_page(raw)
    assert p["attachments"] == [{"id": "abc", "name": "Sunum.pdf", "ext": "pdf"}]
    assert p["text"].startswith("Yeni sözleşme —") and "250 milyon TL" in p["text"]


def test_select_outlook_text_prefers_forward_looking_pages():
    pages = ["Genel kurul tutanağı " * 30, "2027 hedefimiz kapasite artışı ve yeni yatırım planı " * 10, "1 2 3 4 " * 200]
    sel = kap.select_outlook_text(pages, budget=5000)
    assert "[sayfa 2]" in sel and "[sayfa 1]" not in sel


# ---------------- programatik doğrulama ----------------
def test_quote_and_number_checks():
    src = "Şirketimiz 2027 yılında kapasitesini %20 artırmayı HEDEFLEMEKTEDİR. Yatırım tutarı 1.250 milyon TL'dir."
    assert dr.quote_ok("kapasitesini %20 artırmayı hedeflemektedir", src)
    assert not dr.quote_ok("kapasitesini %30 artırmayı hedeflemektedir", src)
    assert not dr.quote_ok("kısa", src)
    assert dr.numbers_ok("2027'de kapasite %20 artacak, yatırım 1250 milyon TL", src)
    assert not dr.numbers_ok("2028'de kapasite %20 artacak", src)


# ---------------- belge okuyucu uçtan uca (sahte KAP + sahte LLM) ----------------
LIST = _list_page([
    _disc(30, "20.11.2026 10:00:00", "Özel Durum Açıklaması (Genel)", "Yeni İhale"),
    _disc(29, "18.11.2026 10:00:00", "Sorumluluk Beyanı", "Sorumluluk Beyanı", cls="FR"),
    _disc(20, "01.11.2026 08:00:00", "Özel Durum Açıklaması (Genel)", "3. Çeyrek Finansal Sonuçlar", att=2),
])
DETAIL_30 = ('Özet Bilgi Yeni İhale İlgili Şirketler [] Açıklamalar Explanations '
             'Şirketimiz 250 milyon TL tutarında savunma ihalesini kazanmıştır. Teslimat 2027 yılında tamamlanacaktır.')
DETAIL_20 = ('Özet Bilgi 3. Çeyrek İlgili Şirketler [] Açıklamalar Explanations Sonuçlar ektedir.'
             '<script>"attachments":[{"objId":"p1","fileName":"Yatirimci Sunumu 3C26.pdf","fileExtension":"pdf"},'
             '{"objId":"p2","fileName":"30.09.2026 IFRS RAPOR.pdf","fileExtension":"pdf"}]</script>')
PDF_TEXT = ["2027 yılı hedefimiz ihracat gelirlerini 2 katına çıkarmaktır. Yeni fabrika yatırımı 2028 yılında "
            "devreye girecektir. Strateji olarak yüksek katma değerli ürünlere odaklanıyoruz. " * 5]


def fake_get(url):
    if "member/filter" in url:
        return json.dumps([{"mkkMemberOid": "oid1", "title": "ACME A.Ş."}]).encode()
    if "bildirim-sorgu-sonuc" in url:
        return LIST.encode()
    if url.endswith("/Bildirim/30"):
        return DETAIL_30.encode()
    if url.endswith("/Bildirim/20"):
        return DETAIL_20.encode()
    if "file/download/p1" in url:
        return b"%PDF-fake"
    return None


class DocScript:
    def __init__(self):
        self.calls = 0

    def __call__(self, schema, system, user):
        self.calls += 1
        if schema is DisclosureDigest:
            return DisclosureDigest(notes=[
                DisclosureNote(disclosure_index=30, impact="olumlu", materiality="yuksek",
                               summary="250 milyon TL tutarında savunma ihalesi kazanıldı, teslimat 2027.",
                               evidence_quote="250 milyon TL tutarında savunma ihalesini kazanmıştır"),
                DisclosureNote(disclosure_index=30, impact="olumlu", materiality="orta",
                               summary="Uydurma: 900 milyon TL ek sipariş.",  # metinde yok -> atılmalı
                               evidence_quote="900 milyon TL ek sipariş alınmıştır"),
            ])
        if schema is OutlookDigest:
            assert "IFRS" not in user  # finansal tablo eki okunmamalı
            return OutlookDigest(summary="Şirket 2027'de ihracatı büyütmeyi hedefliyor.", items=[
                OutlookItem(category="hedef_beklenti", statement="İhracat gelirlerini 2027'de 2 katına çıkarma hedefi",
                            horizon="2027", source="Yatirimci Sunumu 3C26.pdf",
                            evidence_quote="2027 yılı hedefimiz ihracat gelirlerini 2 katına çıkarmaktır"),
                OutlookItem(category="yatirim_kapasite", statement="Yeni fabrika 2030'da açılacak",  # yanlış yıl
                            horizon="2030", source="Yatirimci Sunumu 3C26.pdf",
                            evidence_quote="Yeni fabrika yatırımı 2028 yılında devreye girecektir"),
            ])
        raise AssertionError(schema)


@pytest.fixture
def pdf(monkeypatch):
    monkeypatch.setattr(kap, "pdf_pages", lambda data: PDF_TEXT if data else [])


def test_get_documents_verifies_and_caches(tmp_path, pdf):
    script = DocScript()
    llm = FakeLLM(script)
    cache = DocCache(str(tmp_path / "c.db"))
    out = dr.get_documents(llm, "ACME.IS", get=fake_get, cache=cache, today=datetime(2026, 11, 24))
    k, o = out["kap"], out["outlook"]
    assert k["status"] == "ok" and k["disclosure_count"] == 2  # sorumluluk beyanı atlandı
    assert len(k["notes"]) == 1 and k["rejected_notes"] == 1 and k["notes"][0]["impact"] == "olumlu"
    assert o["status"] == "ok" and [d["name"] for d in o["documents"]] == ["Yatirimci Sunumu 3C26.pdf"]
    assert len(o["items"]) == 1 and o["rejected_items"] == 1 and o["items"][0]["horizon"] == "2027"
    assert all(c["tier"] == "fast" for c in llm.calls)
    # ikinci çağrı önbellekten: LLM'e gidilmez
    n = script.calls
    dr.get_documents(llm, "ACME.IS", get=fake_get, cache=cache, today=datetime(2026, 11, 24))
    assert script.calls == n


def test_get_documents_kap_down_is_veri_yok(tmp_path):
    out = dr.get_documents(FakeLLM(DocScript()), "ACME.IS", get=lambda u: None, cache=DocCache(str(tmp_path / "c.db")))
    assert out["kap"]["status"] == "veri_yok" and out["outlook"]["status"] == "veri_yok"


def test_get_documents_llm_down_keeps_headlines(tmp_path, pdf):
    def down(schema, system, user):
        raise LLMError("402")
    out = dr.get_documents(FakeLLM(down), "ACME.IS", get=fake_get, cache=DocCache(str(tmp_path / "c.db")),
                           today=datetime(2026, 11, 24))
    assert out["kap"]["llm_summary"] == "veri_yok" and len(out["kap"]["headlines"]) == 2
    assert out["outlook"]["status"] == "veri_yok"


# ---------------- analist ham verisi + rapor ----------------
def test_analysts_see_documents_and_trend(ohlcv, tmp_path, pdf):
    b = make_bundle(ohlcv)
    b["financials"] = financials.compute_financial_trend(_income(), _balance(), "USD")
    b["documents"] = dr.get_documents(FakeLLM(DocScript()), "ACME.IS", get=fake_get,
                                      cache=DocCache(str(tmp_path / "c.db")), today=datetime(2026, 11, 24))
    temel = analysts.raw_data_for("temel", b)
    assert temel["financial_trend"]["revenue_yoy_pct"] == 20.0
    assert temel["company_outlook"]["items"][0]["horizon"] == "2027"
    gundem = analysts.raw_data_for("gundem", b)
    assert gundem["kap_disclosures"]["notes"][0]["materiality"] == "yuksek"


def test_workflow_with_document_provider_and_report(provider, tmp_path, pdf):
    docs = dr.get_documents(FakeLLM(DocScript()), "ACME.IS", get=fake_get, cache=DocCache(str(tmp_path / "c.db")),
                            today=datetime(2026, 11, 24))
    seen = {}

    def doc_provider(llm, ticker):
        seen["t"] = ticker
        return docs
    st = run_analysis("TEST.IS", FakeLLM(Script()), data_provider=provider, document_provider=doc_provider)
    assert seen["t"] == "TEST.IS" and st["bundle"]["documents"] is docs
    txt = format_report(st)
    assert "📄 Belgeler" in txt and "savunma ihalesi" in txt and "2 katına" in txt


def test_workflow_document_provider_crash_does_not_break(provider):
    def boom(llm, ticker):
        raise RuntimeError("KAP değişti")
    st = run_analysis("TEST.IS", FakeLLM(Script()), data_provider=provider, document_provider=boom)
    assert st["final"].decision in ("AL", "SAT", "BEKLE")
    assert st["bundle"]["documents"]["kap"]["status"] == "veri_yok"


@pytest.fixture
def provider(ohlcv):
    def _p(t):
        b = make_bundle(ohlcv, ticker=t)
        ind = b["market"]["indicators"]
        ind["levels"]["resistance_60d"] = round(ind["close"] + 8 * ind["atr14"], 4)
        return b
    return _p
