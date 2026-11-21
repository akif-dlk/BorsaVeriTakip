"""SQLite hafıza: tahmin kaydı, doğrulama logları ve 5/10 işlem günlük performans kıyaslaması."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd

from config.settings import settings
from verification.schemas import FinalDecision

HORIZONS = (5, 10)
PriceFetcher = Callable[[str, str], pd.Series]  # (ticker, başlangıç tarihi) -> tarih indeksli kapanış serisi

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    data_date TEXT NOT NULL,
    decision TEXT NOT NULL,
    llm_decision TEXT NOT NULL,
    confidence REAL NOT NULL,
    raw_confidence REAL NOT NULL,
    close REAL,
    entry_low REAL, entry_high REAL, stop_loss REAL, target REAL, risk_reward REAL,
    rationale TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(id),
    stage TEXT NOT NULL,
    analyst TEXT,
    passed INTEGER NOT NULL,
    attempts INTEGER,
    details TEXT
);
CREATE TABLE IF NOT EXISTS evaluations (
    prediction_id INTEGER NOT NULL REFERENCES predictions(id),
    horizon_days INTEGER NOT NULL,
    eval_date TEXT NOT NULL,
    eval_close REAL NOT NULL,
    return_pct REAL NOT NULL,
    success INTEGER,
    PRIMARY KEY (prediction_id, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_pred_ticker ON predictions(ticker, data_date);
"""


def default_db_path() -> str:
    return settings.db_path


def yfinance_closes(ticker: str, start: str) -> pd.Series:
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).history(start=start, interval="1d", auto_adjust=False)
    except Exception:
        return pd.Series(dtype=float)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    s = df["Close"].dropna()
    s.index = [d.date() for d in s.index]
    return s


def judge(decision: str, ret_pct: float) -> int | None:
    if decision == "AL":
        return int(ret_pct > 0)
    if decision == "SAT":
        return int(ret_pct < 0)
    return None  # BEKLE başarı oranına dahil edilmez


class PredictionStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_db_path()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    # ---------- yazma ----------
    def save_prediction(self, f: FinalDecision, state: dict[str, Any] | None = None) -> int:
        p = f.trade_plan
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO predictions (created_at, ticker, data_date, decision, llm_decision, confidence,
                   raw_confidence, close, entry_low, entry_high, stop_loss, target, risk_reward, rationale, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(), f.ticker, f.data_date, f.decision, f.llm_decision,
                 f.confidence, f.raw_confidence, f.close,
                 p.entry_low if p else None, p.entry_high if p else None, p.stop_loss if p else None,
                 p.target if p else None, p.risk_reward if p else None, f.rationale, f.model_dump_json()),
            )
            pid = int(cur.lastrowid)
            if state:
                for name, chk in state.get("checks", {}).items():
                    c.execute("INSERT INTO verification_logs (prediction_id, stage, analyst, passed, attempts, details) "
                              "VALUES (?,?,?,?,?,?)",
                              (pid, "fact_check", name, int(chk.passed), chk.attempts, json.dumps(chk.errors, ensure_ascii=False)))
                for v in state.get("debate", []):
                    c.execute("INSERT INTO verification_logs (prediction_id, stage, analyst, passed, attempts, details) "
                              "VALUES (?,?,?,?,?,?)",
                              (pid, f"debate_r{v.turn.round}", v.turn.side, int(bool(v.accepted)), None,
                               json.dumps({"accepted": len(v.accepted), "rejected": v.rejected}, ensure_ascii=False)))
                c.execute("INSERT INTO verification_logs (prediction_id, stage, analyst, passed, attempts, details) "
                          "VALUES (?,?,?,?,?,?)",
                          (pid, "risk_gate", None, int(f.decision == f.llm_decision), None,
                           json.dumps(f.gate_reasons, ensure_ascii=False)))
        return pid

    def evaluate_due(self, price_fetcher: PriceFetcher = yfinance_closes, today: date | None = None) -> int:
        """Vadesi gelmiş (5/10 işlem günü geçmiş) tahminleri gerçekleşen fiyatla kıyasla. Eklenen kayıt sayısını döner."""
        today = today or date.today()
        added = 0
        with self._conn() as c:
            rows = c.execute(
                """SELECT p.* FROM predictions p WHERE p.close IS NOT NULL AND p.data_date != 'veri_yok' AND
                   (SELECT COUNT(*) FROM evaluations e WHERE e.prediction_id = p.id) < ?""", (len(HORIZONS),)).fetchall()
            for r in rows:
                closes = price_fetcher(r["ticker"], r["data_date"])
                if closes is None or closes.empty:
                    continue
                base = date.fromisoformat(r["data_date"])
                after = closes[[d > base and d <= today for d in closes.index]]
                for h in HORIZONS:
                    if len(after) < h:
                        continue
                    d, px = after.index[h - 1], float(after.iloc[h - 1])
                    ret = (px / r["close"] - 1) * 100
                    cur = c.execute(
                        "INSERT OR IGNORE INTO evaluations VALUES (?,?,?,?,?,?)",
                        (r["id"], h, str(d), px, round(ret, 3), judge(r["decision"], ret)))
                    added += cur.rowcount
        return added

    # ---------- okuma ----------
    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                d = {k: r[k] for k in r.keys() if k != "payload"}
                evs = c.execute("SELECT horizon_days, return_pct, success FROM evaluations WHERE prediction_id=?",
                                (r["id"],)).fetchall()
                d["evaluations"] = {e["horizon_days"]: {"return_pct": e["return_pct"], "success": e["success"]} for e in evs}
                out.append(d)
            return out

    def stats(self) -> dict[int, dict[str, Any]]:
        with self._conn() as c:
            out = {}
            for h in HORIZONS:
                r = c.execute("SELECT COUNT(*) n, SUM(success) s, AVG(return_pct) avg FROM evaluations "
                              "WHERE horizon_days=? AND success IS NOT NULL", (h,)).fetchone()
                n = r["n"] or 0
                out[h] = {"n": n, "success_rate": (r["s"] / n) if n else None, "avg_return_pct": r["avg"]}
            return out

    def verification_logs(self, prediction_id: int) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM verification_logs WHERE prediction_id=?", (prediction_id,))]
