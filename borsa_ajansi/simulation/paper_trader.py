"""Sanal portföy (paper trading): gerçek para YOK, emirler simüle edilir.

Kurallar (hepsi Python'da, deterministik):
- Yalnızca uzun pozisyon (AL). SAT kararı açık pozisyonu kapatır; açığa satış yok.
- Pozisyon büyüklüğü: işlem başına öz sermayenin %SIM_RISK_PER_TRADE_PCT'si riske edilir
  (adet = risk tutarı / (giriş - stop)); tek pozisyon öz sermayenin %SIM_MAX_POSITION_PCT'sini aşamaz; nakit sınırı.
- Komisyon (alış+satış) ve kayma (slippage) uygulanır.
- Stop/hedef günlük en düşük/en yüksek ile kontrol edilir; aynı gün ikisi de değerse STOP kabul edilir (muhafazakâr).
  Fiyat stop'un altında açılırsa açılış fiyatından çıkılır (gap riski).
- Simülasyon süresi dolunca tüm pozisyonlar son fiyattan kapatılır.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from config.settings import settings

log = logging.getLogger(__name__)
BENCHMARK = "XU100.IS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    status TEXT NOT NULL,              -- active | finished | stopped
    bench_start REAL
);
CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES sim_runs(id),
    ticker TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_date TEXT NOT NULL,
    stop_loss REAL NOT NULL,
    target REAL NOT NULL,
    entry_commission REAL NOT NULL,
    confidence REAL,
    rationale TEXT,
    status TEXT NOT NULL,              -- open | closed
    last_bar_date TEXT,
    exit_price REAL,
    exit_date TEXT,
    exit_reason TEXT,
    exit_commission REAL,
    pnl REAL,
    pnl_pct REAL
);
CREATE TABLE IF NOT EXISTS sim_equity (
    run_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    bench_close REAL,
    PRIMARY KEY (run_id, date)
);
CREATE TABLE IF NOT EXISTS sim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class PriceFeed(Protocol):
    def daily(self, ticker: str, start: date) -> pd.DataFrame: ...  # index: date, kolonlar: open high low close
    def last(self, ticker: str) -> float | None: ...


class YFinanceFeed:
    def daily(self, ticker: str, start: date) -> pd.DataFrame:
        import yfinance as yf

        try:
            df = yf.Ticker(ticker).history(start=start.isoformat(), interval="1d", auto_adjust=False)
        except Exception as exc:
            log.warning("Fiyat alınamadı %s: %s", ticker, exc)
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
        df.index = [d.date() for d in df.index]
        return df

    def last(self, ticker: str) -> float | None:
        df = self.daily(ticker, date.today() - timedelta(days=10))
        return float(df["close"].iloc[-1]) if not df.empty else None


def today_tr() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


@dataclass
class Order:
    ticker: str
    qty: int
    price: float
    commission: float


def size_position(equity: float, cash: float, price: float, stop: float) -> int:
    """Risk bazlı adet hesabı (tam sayı lot)."""
    fill = price * (1 + settings.sim_slippage_pct / 100)
    risk_per_share = fill - stop
    if risk_per_share <= 0 or fill <= 0:
        return 0
    by_risk = math.floor(equity * settings.sim_risk_per_trade_pct / 100 / risk_per_share)
    by_cap = math.floor(equity * settings.sim_max_position_pct / 100 / fill)
    by_cash = math.floor(cash / (fill * (1 + settings.sim_commission_pct / 100)))
    return max(0, min(by_risk, by_cap, by_cash))


class PaperTrader:
    def __init__(self, db_path: str | None = None, feed: PriceFeed | None = None,
                 analyze_fn: Callable[[str], dict] | None = None,
                 screen_fn: Callable[..., list[dict]] | None = None,
                 clock: Callable[[], date] = today_tr):
        self.db_path = db_path or settings.db_path
        self.feed = feed or YFinanceFeed()
        self.analyze_fn = analyze_fn
        self.screen_fn = screen_fn
        self.clock = clock
        # Uzun süren döngüde ara bilgi (Telegram'a anlık mesaj); varsayılan sessiz
        self.progress: Callable[[str], None] = lambda msg: None
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _event(self, c, run_id: int, msg: str) -> str:
        c.execute("INSERT INTO sim_events (run_id, ts, message) VALUES (?,?,?)",
                  (run_id, datetime.now(timezone.utc).isoformat(), msg))
        log.info("[sim %s] %s", run_id, msg)
        return msg

    # ------------------------------------------------------------- yaşam döngüsü
    def active_run(self) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sim_runs WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None

    def last_run(self) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sim_runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None

    def start(self, initial_cash: float | None = None, days: int | None = None) -> dict:
        if self.active_run():
            raise RuntimeError("Zaten aktif bir simülasyon var. Önce /sim_durdur.")
        cash = float(initial_cash or settings.sim_initial_cash)
        days = int(days or settings.sim_days)
        today = self.clock()
        bench = self.feed.last(BENCHMARK)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO sim_runs (created_at, start_date, end_date, initial_cash, cash, status, bench_start) "
                "VALUES (?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), today.isoformat(), (today + timedelta(days=days)).isoformat(),
                 cash, cash, "active", bench))
            rid = int(cur.lastrowid)
            self._event(c, rid, f"Simülasyon başladı: {cash:,.0f} TL, {days} gün (bitiş {today + timedelta(days=days)})")
        return self.active_run()

    def stop(self) -> dict | None:
        run = self.active_run()
        if not run:
            return None
        self._close_all(run, "simülasyon durduruldu")
        with self._conn() as c:
            c.execute("UPDATE sim_runs SET status='stopped' WHERE id=?", (run["id"],))
        self.snapshot(run["id"])
        return self.last_run()

    # ------------------------------------------------------------- emirler
    def open_trades(self, run_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sim_trades WHERE run_id=? AND status='open'", (run_id,))]

    def closed_trades(self, run_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM sim_trades WHERE run_id=? AND status='closed' ORDER BY id", (run_id,))]

    def equity(self, run: dict) -> tuple[float, dict[str, float]]:
        prices: dict[str, float] = {}
        value = 0.0
        for t in self.open_trades(run["id"]):
            p = self.feed.last(t["ticker"])
            p = p if p is not None else t["entry_price"]
            prices[t["ticker"]] = p
            value += p * t["qty"]
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
        return cash + value, prices

    def buy(self, run: dict, ticker: str, price: float, stop: float, target: float,
            confidence: float | None = None, rationale: str = "") -> str | None:
        eq, _ = self.equity(run)
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
            qty = size_position(eq, cash, price, stop)
            if qty < 1:
                return self._event(c, run["id"], f"{ticker}: AL sinyali var ama pozisyon büyüklüğü 0 (nakit/risk sınırı)")
            fill = round(price * (1 + settings.sim_slippage_pct / 100), 4)
            comm = round(fill * qty * settings.sim_commission_pct / 100, 2)
            c.execute("UPDATE sim_runs SET cash = cash - ? WHERE id=?", (fill * qty + comm, run["id"]))
            c.execute(
                "INSERT INTO sim_trades (run_id, ticker, qty, entry_price, entry_date, stop_loss, target, entry_commission, "
                "confidence, rationale, status, last_bar_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run["id"], ticker, qty, fill, self.clock().isoformat(), stop, target, comm, confidence, rationale,
                 "open", self.clock().isoformat()))
            return self._event(c, run["id"], f"🟢 ALIŞ {ticker}: {qty} adet @ {fill:.2f} = {fill * qty:,.0f} TL "
                                             f"(stop {stop:.2f}, hedef {target:.2f}, komisyon {comm:.2f})")

    def sell(self, trade: dict, price: float, reason: str, on: date | None = None) -> str:
        fill = round(price * (1 - settings.sim_slippage_pct / 100), 4)
        comm = round(fill * trade["qty"] * settings.sim_commission_pct / 100, 2)
        cost = trade["entry_price"] * trade["qty"] + trade["entry_commission"]
        proceeds = fill * trade["qty"] - comm
        pnl = proceeds - cost
        with self._conn() as c:
            c.execute("UPDATE sim_runs SET cash = cash + ? WHERE id=?", (proceeds, trade["run_id"]))
            c.execute("UPDATE sim_trades SET status='closed', exit_price=?, exit_date=?, exit_reason=?, exit_commission=?, "
                      "pnl=?, pnl_pct=? WHERE id=?",
                      (fill, (on or self.clock()).isoformat(), reason, comm, round(pnl, 2), round(pnl / cost * 100, 2),
                       trade["id"]))
            icon = "✅" if pnl >= 0 else "🔻"
            return self._event(c, trade["run_id"], f"{icon} SATIŞ {trade['ticker']}: {trade['qty']} adet @ {fill:.2f} "
                                                   f"({reason}) → K/Z {pnl:+,.0f} TL (%{pnl / cost * 100:+.2f})")

    # ------------------------------------------------------------- günlük döngü
    def check_exits(self) -> list[str]:
        """Giriş gününden sonraki günlük barlarla stop/hedef kontrolü."""
        run = self.active_run()
        if not run:
            return []
        msgs = []
        for t in self.open_trades(run["id"]):
            last_seen = date.fromisoformat(t["last_bar_date"])
            bars = self.feed.daily(t["ticker"], last_seen)
            bars = bars[[d > last_seen for d in bars.index]] if not bars.empty else bars
            exited = False
            for d, b in bars.iterrows():
                if b["low"] <= t["stop_loss"]:
                    msgs.append(self.sell(t, min(b["open"], t["stop_loss"]), "stop-loss", d))
                    exited = True
                    break
                if b["high"] >= t["target"]:
                    msgs.append(self.sell(t, max(b["open"], t["target"]), "hedef", d))
                    exited = True
                    break
            if not exited and not bars.empty:
                with self._conn() as c:
                    c.execute("UPDATE sim_trades SET last_bar_date=? WHERE id=?", (bars.index[-1].isoformat(), t["id"]))
        msgs += self._expire_if_due()
        return msgs

    def _close_all(self, run: dict, reason: str) -> list[str]:
        msgs = []
        for t in self.open_trades(run["id"]):
            p = self.feed.last(t["ticker"]) or t["entry_price"]
            msgs.append(self.sell(t, p, reason))
        return msgs

    def _expire_if_due(self) -> list[str]:
        run = self.active_run()
        if not run or self.clock() < date.fromisoformat(run["end_date"]):
            return []
        msgs = self._close_all(run, "süre doldu")
        with self._conn() as c:
            c.execute("UPDATE sim_runs SET status='finished' WHERE id=?", (run["id"],))
            msgs.append(self._event(c, run["id"], "🏁 Simülasyon süresi doldu, tüm pozisyonlar kapatıldı."))
        self.snapshot(run["id"])
        return msgs

    def trade_cycle(self) -> list[str]:
        """Açık pozisyonları yeniden değerlendir + tarama ile yeni adaylara AL emri ver (LLM kullanır)."""
        msgs = self.check_exits()
        run = self.active_run()
        if not run:
            return msgs
        assert self.analyze_fn is not None and self.screen_fn is not None

        # 1) Açık pozisyonlar: SAT -> kapat, AL -> stop'u yukarı çek (asla aşağı değil)
        for t in self.open_trades(run["id"]):
            self.progress(f"🔁 Açık pozisyon {t['ticker']} yeniden değerlendiriliyor...")
            try:
                f = self.analyze_fn(t["ticker"])["final"]
            except Exception as exc:
                msgs.append(f"⚠️ {t['ticker']} yeniden analiz başarısız: {exc}")
                continue
            if f.decision == "SAT":
                p = self.feed.last(t["ticker"]) or t["entry_price"]
                msgs.append(self.sell(t, p, "SAT sinyali"))
            elif f.decision == "AL" and f.trade_plan and f.trade_plan.stop_loss > t["stop_loss"]:
                with self._conn() as c:
                    c.execute("UPDATE sim_trades SET stop_loss=? WHERE id=?", (f.trade_plan.stop_loss, t["id"]))
                    msgs.append(self._event(c, run["id"], f"⬆️ {t['ticker']} stop yükseltildi: "
                                                          f"{t['stop_loss']:.2f} → {f.trade_plan.stop_loss:.2f}"))

        # 2) Yeni adaylar
        held = {t["ticker"] for t in self.open_trades(run["id"])}
        slots = settings.sim_max_positions - len(held)
        if slots <= 0:
            msgs.append("Maksimum pozisyon sayısına ulaşıldı, yeni alım yok.")
            return msgs
        self.progress("🔍 BIST100 taranıyor (teknik puanlama, LLM yok)...")
        candidates = self.screen_fn(exclude=held)
        line = "🔍 Tarama adayları: " + (", ".join(f"{c['ticker']}({c['score']})" for c in candidates) or "yok")
        msgs.append(line)
        self.progress(line)
        for i, cand in enumerate(candidates, 1):
            if slots <= 0:
                break
            tk = cand["ticker"]
            self.progress(f"🧠 {tk} analiz ediliyor ({i}/{len(candidates)}): belgeler, 4 analist, tartışma, risk. "
                          f"Model yoğunsa birkaç dakika sürebilir...")
            try:
                f = self.analyze_fn(tk)["final"]
            except Exception as exc:
                msgs.append(f"⚠️ {tk} analiz başarısız: {exc}")
                continue
            if f.decision != "AL" or not f.trade_plan:
                msgs.append(f"⏸️ {tk}: {f.decision} (güven %{f.confidence * 100:.0f}) — alım yok")
                continue
            price = self.feed.last(tk)
            if price is None:
                msgs.append(f"⚠️ {tk}: güncel fiyat yok")
                continue
            if price > f.trade_plan.entry_high * 1.01:
                msgs.append(f"⏸️ {tk}: fiyat {price:.2f} giriş bölgesinin ({f.trade_plan.entry_high:.2f}) üstünde, kovalanmadı")
                continue
            if price <= f.trade_plan.stop_loss:
                msgs.append(f"⏸️ {tk}: fiyat stop seviyesinin altında, alım yok")
                continue
            msg = self.buy(run, tk, price, f.trade_plan.stop_loss, f.trade_plan.target, f.confidence, f.rationale[:300])
            msgs.append(msg)
            if msg and msg.startswith("🟢"):
                slots -= 1
        self.snapshot()
        return msgs

    def snapshot(self, run_id: int | None = None) -> None:
        run = self.active_run() if run_id is None else self._run(run_id)
        if not run:
            return
        eq, _ = self.equity(run)
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
            c.execute("INSERT OR REPLACE INTO sim_equity VALUES (?,?,?,?,?)",
                      (run["id"], self.clock().isoformat(), round(eq, 2), round(cash, 2), self.feed.last(BENCHMARK)))

    def _run(self, run_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sim_runs WHERE id=?", (run_id,)).fetchone()
            return dict(r) if r else None

    # ------------------------------------------------------------- rapor
    def report(self, run_id: int | None = None) -> str:
        run = self._run(run_id) if run_id else (self.active_run() or self.last_run())
        if not run:
            return "Henüz simülasyon yok. Başlatmak için: /sim_baslat 10000 14"
        eq, prices = self.equity(run)
        init = run["initial_cash"]
        ret = (eq / init - 1) * 100
        bench_now = self.feed.last(BENCHMARK)
        if run["status"] != "active":  # biten simülasyon: bitiş anındaki endeks değeri
            with self._conn() as c:
                row = c.execute("SELECT bench_close FROM sim_equity WHERE run_id=? ORDER BY date DESC LIMIT 1",
                                (run["id"],)).fetchone()
            bench_now = row["bench_close"] if row and row["bench_close"] else bench_now
        bench_ret = ((bench_now / run["bench_start"] - 1) * 100) if bench_now and run["bench_start"] else None
        today = self.clock()
        start, end = date.fromisoformat(run["start_date"]), date.fromisoformat(run["end_date"])
        status = {"active": "🟢 aktif", "finished": "🏁 tamamlandı", "stopped": "⏹️ durduruldu"}[run["status"]]
        lines = [
            f"💼 Sanal Portföy #{run['id']} — {status}",
            f"Dönem: {start} → {end} (gün {min((today - start).days, (end - start).days)}/{(end - start).days})",
            "",
            f"Başlangıç: {init:,.0f} TL",
            f"Şu an:     {eq:,.0f} TL  ({ret:+.2f}%)",
            f"BIST100 aynı dönem: {bench_ret:+.2f}%" if bench_ret is not None else "BIST100 aynı dönem: veri_yok",
        ]
        if bench_ret is not None:
            lines.append(f"Endekse göre fark: {ret - bench_ret:+.2f} puan")
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
        lines.append(f"Nakit: {cash:,.0f} TL")

        opens = self.open_trades(run["id"])
        if opens:
            lines += ["", "📂 Açık pozisyonlar"]
            for t in opens:
                p = prices.get(t["ticker"], t["entry_price"])
                u = (p - t["entry_price"]) * t["qty"]
                lines.append(f"• {t['ticker']} {t['qty']} adet | giriş {t['entry_price']:.2f} → {p:.2f} "
                             f"({(p / t['entry_price'] - 1) * 100:+.2f}%, {u:+,.0f} TL) | stop {t['stop_loss']:.2f} hedef {t['target']:.2f}")
        closed = self.closed_trades(run["id"])
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            realized = sum(t["pnl"] for t in closed)
            comm = sum((t["entry_commission"] or 0) + (t["exit_commission"] or 0) for t in closed)
            lines += ["", f"📕 Kapanan işlemler: {len(closed)} | kazanan {len(wins)} (%{len(wins) / len(closed) * 100:.0f}) "
                          f"| gerçekleşen K/Z {realized:+,.0f} TL | komisyon {comm:,.0f} TL"]
            for t in closed[-8:]:
                lines.append(f"• {t['ticker']} {t['entry_date']}→{t['exit_date']} {t['exit_reason']}: "
                             f"{t['pnl']:+,.0f} TL ({t['pnl_pct']:+.2f}%)")
        lines += ["", "ℹ️ Simülasyondur; gerçek emir verilmez. Yatırım tavsiyesi değildir."]
        return "\n".join(lines)

    def recent_events(self, limit: int = 15) -> list[str]:
        run = self.active_run() or self.last_run()
        if not run:
            return []
        with self._conn() as c:
            return [r["message"] for r in c.execute(
                "SELECT message FROM sim_events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run["id"], limit))][::-1]
