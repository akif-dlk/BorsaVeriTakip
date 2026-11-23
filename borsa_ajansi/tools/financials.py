"""Çeyreklik finansal tablo trendi: gelir tablosu + bilanço (yfinance). Tüm oranlar Python'da hesaplanır.

Çıktı temel analiste verilir; analist yalnızca yorumlar. Kaynak yoksa / alan eksikse 'veri_yok'.
Not: TL raporlayan şirketlerde yıllık (YoY) büyüme enflasyon etkisi taşır; USD raporlayanlarda (ör. THYAO) taşımaz.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from config.settings import VERI_YOK
from tools.market_data import _clean, normalize_ticker

log = logging.getLogger(__name__)

_INCOME_ROWS = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "gross_profit": ["Gross Profit"],
    "operating_income": ["Operating Income", "Total Operating Income As Reported"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "net_income": ["Net Income Common Stockholders", "Net Income"],
}
_BALANCE_ROWS = {
    "total_debt": ["Total Debt"],
    "net_debt": ["Net Debt"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "equity": ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"],
}


def fetch_quarterly_statements(ticker: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    try:
        t = yf.Ticker(ticker)
        return {"income": t.quarterly_income_stmt, "balance": t.quarterly_balance_sheet}
    except Exception as exc:  # ağ / kaynak hatası -> veri_yok
        log.warning("Çeyreklik tablolar alınamadı %s: %s", ticker, exc)
        return {}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _series(df: pd.DataFrame | None, names: list[str]) -> pd.Series | None:
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            if s.notna().any():
                return s
    return None


def _val(s: pd.Series | None, col) -> float | None:
    if s is None or col not in s.index:
        return None
    v = s[col]
    return float(v) if pd.notna(v) else None


def _pct(new: float | None, old: float | None) -> Any:
    if not (_is_num(new) and _is_num(old)) or old == 0:
        return VERI_YOK
    return _clean((new - old) / abs(old) * 100, 2)


def _margin(num: float | None, rev: float | None) -> Any:
    if not (_is_num(num) and _is_num(rev)) or rev <= 0:
        return VERI_YOK
    return _clean(num / rev * 100, 2)


def _or(v: Any) -> Any:
    return _clean(v, 0) if _is_num(v) else VERI_YOK


def compute_financial_trend(income: pd.DataFrame | None, balance: pd.DataFrame | None,
                            currency: str | None = None) -> dict[str, Any]:
    """Saf fonksiyon: çeyreklik tablolar -> trend metrikleri (son çeyrek, önceki çeyrek, geçen yılın aynı çeyreği)."""
    inc = {k: _series(income, v) for k, v in _INCOME_ROWS.items()}
    bal = {k: _series(balance, v) for k, v in _BALANCE_ROWS.items()}
    rev = inc["revenue"]
    if rev is None:
        return {"status": VERI_YOK}
    cols = sorted([c for c in rev.index if pd.notna(rev[c])], reverse=True)  # en yeni önce
    if not cols:
        return {"status": VERI_YOK}
    q0 = cols[0]
    q1 = cols[1] if len(cols) > 1 else None
    # Geçen yılın aynı çeyreği: ~1 yıl önceki tarih (±45 gün)
    q4 = next((c for c in cols[1:] if abs((pd.Timestamp(q0) - pd.Timestamp(c)).days - 365) <= 45), None)

    def g(name: str, col) -> float | None:
        return _val(inc[name], col) if col is not None else None

    out: dict[str, Any] = {
        "status": "ok",
        "latest_quarter": pd.Timestamp(q0).strftime("%Y-%m-%d"),
        "quarters_available": len(cols),
        "reporting_currency": currency or VERI_YOK,
        "latest": {k: _or(g(k, q0)) for k in _INCOME_ROWS},
        "revenue_qoq_pct": _pct(g("revenue", q0), g("revenue", q1)),
        "revenue_yoy_pct": _pct(g("revenue", q0), g("revenue", q4)),
        "net_income_yoy_pct": _pct(g("net_income", q0), g("net_income", q4)),
        "ebitda_yoy_pct": _pct(g("ebitda", q0), g("ebitda", q4)),
        "net_margin_pct": _margin(g("net_income", q0), g("revenue", q0)),
        "net_margin_year_ago_pct": _margin(g("net_income", q4), g("revenue", q4)),
        "ebitda_margin_pct": _margin(g("ebitda", q0), g("revenue", q0)),
        "ebitda_margin_year_ago_pct": _margin(g("ebitda", q4), g("revenue", q4)),
        "gross_margin_pct": _margin(g("gross_profit", q0), g("revenue", q0)),
    }
    for a, b, key in (("net_margin_pct", "net_margin_year_ago_pct", "net_margin_change_pts"),
                      ("ebitda_margin_pct", "ebitda_margin_year_ago_pct", "ebitda_margin_change_pts")):
        out[key] = _clean(out[a] - out[b], 2) if _is_num(out[a]) and _is_num(out[b]) else VERI_YOK

    # Son 4 çeyrek toplamı (TTM) ve kâr eden çeyrek sayısı
    last4 = cols[:4]
    if len(last4) == 4:
        r = [g("revenue", c) for c in last4]
        n = [g("net_income", c) for c in last4]
        out["ttm_revenue"] = _or(sum(r)) if all(_is_num(x) for x in r) else VERI_YOK
        out["ttm_net_income"] = _or(sum(n)) if all(_is_num(x) for x in n) else VERI_YOK
        e = [g("ebitda", c) for c in last4]
        out["ttm_ebitda"] = _or(sum(e)) if all(_is_num(x) for x in e) else VERI_YOK
        out["profitable_quarters_last4"] = sum(1 for x in n if _is_num(x) and x > 0)
    else:
        out["ttm_revenue"] = out["ttm_net_income"] = out["ttm_ebitda"] = out["profitable_quarters_last4"] = VERI_YOK

    # Bilanço: son durum ve ~1 yıl önceye göre değişim
    bcols = []
    for s in bal.values():
        if s is not None:
            bcols = sorted([c for c in s.index if pd.notna(s[c])], reverse=True)
            break
    if bcols:
        b0 = bcols[0]
        b4 = next((c for c in bcols[1:] if abs((pd.Timestamp(b0) - pd.Timestamp(c)).days - 365) <= 45), None)
        debt, cash, eq = (_val(bal[k], b0) for k in ("total_debt", "cash", "equity"))
        nd = _val(bal["net_debt"], b0)
        if nd is None and _is_num(debt) and _is_num(cash):
            nd = debt - cash
        nd_prev = _val(bal["net_debt"], b4) if b4 is not None else None
        out["balance"] = {
            "date": pd.Timestamp(b0).strftime("%Y-%m-%d"),
            "total_debt": _or(debt), "cash": _or(cash), "equity": _or(eq), "net_debt": _or(nd),
            "net_debt_yoy_pct": _pct(nd, nd_prev),
            "debt_to_equity": _clean(debt / eq, 2) if _is_num(debt) and _is_num(eq) and eq > 0 else VERI_YOK,
            "equity_yoy_pct": _pct(eq, _val(bal["equity"], b4) if b4 is not None else None),
        }
    else:
        out["balance"] = {"status": VERI_YOK}

    out["missing_fields"] = [k for k, v in out.items() if v == VERI_YOK]
    return out


def get_financial_trend(ticker: str, fetcher: Callable[[str], dict] = fetch_quarterly_statements,
                        currency: str | None = None) -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    st = fetcher(ticker) or {}
    data = compute_financial_trend(st.get("income"), st.get("balance"), currency)
    data["ticker"] = ticker
    return data


def reconcile_fundamentals(fund: dict[str, Any], trend: dict[str, Any]) -> dict[str, Any]:
    """yfinance özet verisindeki FAVÖK bazen tek çeyrek / farklı dönemdir. Çeyreklik tablolardan hesaplanan son 4 çeyrek
    (TTM) FAVÖK varsa onu esas alır ve net borç/FAVÖK'ü yeniden hesaplar; büyük sapmayı şeffafça kaydeder."""
    ttm = trend.get("ttm_ebitda") if trend.get("status") == "ok" else None
    if fund.get("status") != "ok" or not _is_num(ttm) or ttm <= 0:
        return fund
    out = dict(fund)
    old = out.get("ebitda")
    if _is_num(old) and abs(old - ttm) / ttm > 0.25:
        out["suspicious_values_removed"] = list(out.get("suspicious_values_removed", [])) + [
            f"ebitda={old} (son 4 çeyrek toplamı {ttm} ile çelişiyor, TTM kullanıldı)"]
    out["ebitda"] = ttm
    out["ebitda_basis"] = "son_4_ceyrek_ttm"
    nd = out.get("net_debt")
    out["net_debt_to_ebitda"] = _clean(nd / ttm, 2) if _is_num(nd) else VERI_YOK
    return out
