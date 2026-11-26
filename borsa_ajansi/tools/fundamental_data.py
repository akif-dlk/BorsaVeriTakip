"""Temel veriler: F/K, PD/DD, FAVÖK, net borç, marjlar.

Birincil kaynak yfinance. Borsa MCP (BIST/KAP/EVDS) entegrasyonu için `fetch_from_borsa_mcp`
bir adaptör noktası olarak bırakıldı; yapılandırılmamışsa sessizce atlanır ve alanlar
uydurulmaz, 'veri_yok' olarak işaretlenir.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from config.settings import VERI_YOK
from tools.market_data import _clean, normalize_ticker

log = logging.getLogger(__name__)

# yfinance info anahtarı -> bizim alan adımız
_FIELD_MAP = {
    "trailingPE": "pe_ratio",
    "forwardPE": "forward_pe",
    "priceToBook": "pb_ratio",
    "ebitda": "ebitda",
    "enterpriseToEbitda": "ev_ebitda",
    "totalDebt": "total_debt",
    "totalCash": "total_cash",
    "marketCap": "market_cap",
    "profitMargins": "profit_margin",
    "returnOnEquity": "roe",
    "revenueGrowth": "revenue_growth",
    "earningsGrowth": "earnings_growth",
    "debtToEquity": "debt_to_equity",
    "dividendYield": "dividend_yield",
    "sector": "sector",
    "industry": "industry",
    "currency": "currency",
    "financialCurrency": "financial_currency",
    "longName": "name",
}


# Makul aralık dışı değerler (ör. yfinance'te TRY/USD karışıklığı) güvenilmez sayılır -> veri_yok
_SANITY_BOUNDS = {
    "pe_ratio": (0, 200), "forward_pe": (0, 200), "pb_ratio": (0, 15), "ev_ebitda": (-50, 60),
    "profit_margin": (-2, 2), "roe": (-3, 3), "debt_to_equity": (0, 1000), "dividend_yield": (0, 30),
}

# BIST sektörel referans medyan çarpanları (F/K, PD/DD) — tek başına F/K yanılgısını önler
BIST_SECTOR_MEDIANS: dict[str, tuple[float, float]] = {
    "banka": (4.8, 1.25),
    "holding": (5.8, 1.10),
    "havacilik": (6.8, 1.40),
    "savunma_teknoloji": (18.5, 4.20),
    "perakende_gida": (13.5, 3.40),
    "enerji": (11.0, 2.10),
    "sanayi_emtia": (9.5, 1.75),
    "otomotiv": (8.5, 2.60),
    "iletisim": (11.5, 2.50),
    "gyo": (5.0, 0.75),
    "genel_bist": (9.0, 1.80),
}

_TICKER_SECTOR_MAP: dict[str, str] = {
    "AKBNK": "banka", "GARAN": "banka", "ISCTR": "banka", "YKBNK": "banka", "VAKBN": "banka",
    "HALKB": "banka", "SKBNK": "banka", "TSKB": "banka", "ANSGR": "banka", "TURSG": "banka", "ISMEN": "banka",
    "KCHOL": "holding", "SAHOL": "holding", "AGHOL": "holding", "DOHOL": "holding", "ALARK": "holding", "BERA": "holding",
    "THYAO": "havacilik", "PGSUS": "havacilik", "TAVHL": "havacilik", "CLEBI": "havacilik",
    "ASELS": "savunma_teknoloji", "ARDYZ": "savunma_teknoloji", "MIATK": "savunma_teknoloji",
    "PATEK": "savunma_teknoloji", "KONTR": "savunma_teknoloji", "REEDR": "savunma_teknoloji", "ALTNY": "savunma_teknoloji",
    "BIMAS": "perakende_gida", "MGROS": "perakende_gida", "SOKM": "perakende_gida", "MAVI": "perakende_gida",
    "CCOLA": "perakende_gida", "AEFES": "perakende_gida", "ULKER": "perakende_gida", "TABGD": "perakende_gida",
    "AKSEN": "enerji", "ENJSA": "enerji", "ENERY": "enerji", "CWENE": "enerji", "ASTOR": "enerji",
    "EUPWR": "enerji", "GESAN": "enerji", "SMRTG": "enerji", "YEOTK": "enerji", "ODAS": "enerji", "ZOREN": "enerji",
    "FROTO": "otomotiv", "TOASO": "otomotiv", "DOAS": "otomotiv", "TTRAK": "otomotiv", "OTKAR": "otomotiv",
    "TCELL": "iletisim", "TTKOM": "iletisim",
    "EKGYO": "gyo", "AVPGY": "gyo",
    "EREGL": "sanayi_emtia", "KRDMD": "sanayi_emtia", "KCAER": "sanayi_emtia", "BRSAN": "sanayi_emtia",
    "TUPRS": "sanayi_emtia", "PETKM": "sanayi_emtia", "SISE": "sanayi_emtia", "AKSA": "sanayi_emtia",
    "SASA": "sanayi_emtia", "CIMSA": "sanayi_emtia", "OYAKC": "sanayi_emtia", "ARCLK": "sanayi_emtia",
}


def resolve_bist_sector(ticker: str | None, yf_sector: Any) -> str:
    sym = (ticker or "").upper().split(".")[0]
    if sym in _TICKER_SECTOR_MAP:
        return _TICKER_SECTOR_MAP[sym]
    s = str(yf_sector or "").lower()
    if "finan" in s:
        return "banka"
    if "tech" in s:
        return "savunma_teknoloji"
    if "defensive" in s or "consumer" in s:
        return "perakende_gida"
    if "utilit" in s or "energy" in s:
        return "enerji"
    if "real estate" in s:
        return "gyo"
    if "communic" in s:
        return "iletisim"
    if "industr" in s or "material" in s:
        return "sanayi_emtia"
    return "genel_bist"


def compute_sector_comparison(out: dict[str, Any], ticker: str | None = None) -> dict[str, Any]:
    group = resolve_bist_sector(ticker, out.get("sector"))
    med_pe, med_pb = BIST_SECTOR_MEDIANS[group]
    pe, pb = out.get("pe_ratio"), out.get("pb_ratio")
    pe_diff = _clean((pe / med_pe - 1) * 100, 1) if isinstance(pe, (int, float)) and med_pe > 0 else VERI_YOK
    pb_diff = _clean((pb / med_pb - 1) * 100, 1) if isinstance(pb, (int, float)) and med_pb > 0 else VERI_YOK
    diffs = [d for d in (pe_diff, pb_diff) if isinstance(d, (int, float))]
    avg_diff = sum(diffs) / len(diffs) if diffs else None
    label = (
        VERI_YOK
        if avg_diff is None
        else "iskontolu"
        if avg_diff <= -15.0
        else "primli"
        if avg_diff >= 20.0
        else "sektore_uyumlu"
    )
    return {
        "bist_sector_group": group,
        "sector_pe_median": med_pe,
        "sector_pb_median": med_pb,
        "pe_vs_sector_pct": pe_diff,
        "pb_vs_sector_pct": pb_diff,
        "valuation_vs_sector": label,
    }


def fetch_yfinance_info(ticker: str) -> dict[str, Any]:
    import yfinance as yf

    try:
        return yf.Ticker(ticker).info or {}
    except Exception as exc:
        log.warning("Temel veri alınamadı %s: %s", ticker, exc)
        return {}


def fetch_from_borsa_mcp(ticker: str) -> dict[str, Any]:
    """Borsa MCP adaptör noktası. Henüz bağlı değil -> boş döner (uydurma veri yok)."""
    return {}


def build_fundamentals(info: dict[str, Any], extra: dict[str, Any] | None = None,
                       ticker: str | None = None) -> dict[str, Any]:
    """Saf fonksiyon: ham info -> normalize temel veri sözlüğü + Python'da hesaplanan türevler."""
    if not info and not extra:
        return {"status": VERI_YOK}
    merged = {**info, **(extra or {})}
    out: dict[str, Any] = {"status": "ok"}
    for src, dst in _FIELD_MAP.items():
        val = merged.get(src, merged.get(dst))
        out[dst] = _clean(val) if isinstance(val, (int, float)) else (val if val not in (None, "") else VERI_YOK)

    suspicious = []
    for k, (lo, hi) in _SANITY_BOUNDS.items():
        v = out.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (lo <= v <= hi):
            suspicious.append(f"{k}={v}")
            out[k] = VERI_YOK
    out["suspicious_values_removed"] = suspicious

    debt, cash, ebitda = out["total_debt"], out["total_cash"], out["ebitda"]
    if isinstance(debt, (int, float)) and isinstance(cash, (int, float)):
        out["net_debt"] = _clean(debt - cash, 0)
        if isinstance(ebitda, (int, float)) and ebitda > 0:
            out["net_debt_to_ebitda"] = _clean((debt - cash) / ebitda, 2)
        else:
            out["net_debt_to_ebitda"] = VERI_YOK
    else:
        out["net_debt"] = VERI_YOK
        out["net_debt_to_ebitda"] = VERI_YOK

    numeric = [k for k, v in out.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numeric:
        return {"status": VERI_YOK}
    out["sector_comparison"] = compute_sector_comparison(out, ticker)
    out["missing_fields"] = [k for k, v in out.items() if v == VERI_YOK]
    return out


def get_fundamentals(
    ticker: str,
    info_fetcher: Callable[[str], dict] = fetch_yfinance_info,
    mcp_fetcher: Callable[[str], dict] = fetch_from_borsa_mcp,
) -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    data = build_fundamentals(info_fetcher(ticker), mcp_fetcher(ticker), ticker=ticker)
    data["ticker"] = ticker
    return data
