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


def build_fundamentals(info: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
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
    out["missing_fields"] = [k for k, v in out.items() if v == VERI_YOK]
    return out


def get_fundamentals(
    ticker: str,
    info_fetcher: Callable[[str], dict] = fetch_yfinance_info,
    mcp_fetcher: Callable[[str], dict] = fetch_from_borsa_mcp,
) -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    data = build_fundamentals(info_fetcher(ticker), mcp_fetcher(ticker))
    data["ticker"] = ticker
    return data
