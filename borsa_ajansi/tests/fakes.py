"""Testler için sahte LLM: gerçek API çağırmadan yapılandırılmış çıktı döndürür."""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable

import pandas as pd

from tools.market_data import compute_indicators, compute_sentiment_metrics


class FakeRunnable:
    def __init__(self, parent: "FakeLLM", schema, tier):
        self.parent, self.schema, self.tier = parent, schema, tier

    def invoke(self, system: str, user: str):
        with self.parent.lock:
            self.parent.calls.append({"schema": self.schema.__name__, "tier": self.tier, "system": system, "user": user})
        return self.parent.responder(self.schema, system, user)


class FakeLLM:
    def __init__(self, responder: Callable[[type, str, str], Any]):
        self.responder = responder
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def with_structured_output(self, schema, tier):
        return FakeRunnable(self, schema, tier)


def extract_raw(user: str) -> dict:
    m = re.search(r"Ham veri \(JSON\):\n(.*?)(\n\n|$)", user, re.S)
    return json.loads(m.group(1))


def extract_analyst(user: str) -> str:
    return re.search(r"Analist: (\w+)", user).group(1)


def make_bundle(df: pd.DataFrame, ticker: str = "TEST.IS", news_ok: bool = True, fundamentals_ok: bool = True) -> dict:
    ind = compute_indicators(df)
    return {
        "ticker": ticker,
        "market": {"ticker": ticker, "indicators": ind, "sentiment_metrics": compute_sentiment_metrics(ind)},
        "fundamentals": {"status": "ok", "ticker": ticker, "pe_ratio": 6.2, "pb_ratio": 1.1, "net_debt": 500.0,
                         "ebitda": 1000.0, "net_debt_to_ebitda": 0.5, "missing_fields": []}
        if fundamentals_ok else {"status": "veri_yok"},
        "news": {"status": "ok", "ticker": ticker, "news_count": 3,
                 "company_news": [{"title": "Şirket yeni uçak siparişi verdi", "published": "2026-01-01"}],
                 "kap_news": [{"title": "KAP: Temettü kararı", "published": "2026-01-01"}]}
        if news_ok else {"status": "veri_yok", "company_news": "veri_yok", "kap_news": "veri_yok"},
        "macro": {"status": "ok", "macro": {"TCMB faiz kararı": [{"title": "TCMB faizi sabit tuttu", "published": "x"}]}},
    }
