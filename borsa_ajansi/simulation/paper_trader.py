"""Sanal portföy (paper trading): gerçek para YOK, emirler simüle edilir.

Kurallar (hepsi Python'da, deterministik):
- Yalnızca uzun pozisyon (AL). SAT kararı açık pozisyonu kapatır; açığa satış yok.
- Pozisyon büyüklüğü: işlem başına öz sermayenin %SIM_RISK_PER_TRADE_PCT'si riske edilir
  (adet = risk tutarı / (giriş - stop)); tek pozisyon öz sermayenin %SIM_MAX_POSITION_PCT'sini aşamaz; nakit sınırı.
- Kademeli yatırım (SIM_TIERS): portföyün ilk %50'si güven>=0.65 & R/Ö>=1.3 sinyallerle, sonraki %25'i
  0.68 & 1.4 ile, kalanı 0.70 & 1.5 ile yatırılabilir. Güçlü sinyal gevşek kademelerin parasını da kullanabilir.
- Daha iyi adaya geçiş: limit/pozisyon/nakit doluysa ve yeni aday eldeki en zayıf pozisyondan belirgin biçimde
  daha güvenilir VE kalan risk/ödülü daha yüksekse o pozisyon satılır, yeni aday alınır.
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
    initial_stop REAL,
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


def tier_limit(confidence: float, risk_reward: float) -> float | None:
    """Sinyalin karşıladığı en sıkı kademenin kümülatif portföy oranı (ör. 0.5 / 0.75 / 1.0); hiçbiri değilse None."""
    ok = [cum for cum, c, rr in settings.sim_tiers if confidence >= c - 1e-9 and risk_reward >= rr - 1e-9]
    return max(ok) if ok else None


def remaining_rr(trade: dict, price: float) -> float:
    """Eldeki pozisyonun şu anki fiyattan kalan risk/ödülü: (hedef - fiyat) / (fiyat - stop)."""
    if price <= trade["stop_loss"]:
        return 0.0
    return max(0.0, (trade["target"] - price) / (price - trade["stop_loss"]))


def size_position(equity: float, cash: float, price: float, stop: float, max_value: float | None = None,
                  risk_mult: float = 1.0) -> int:
    """Risk bazlı adet hesabı (tam sayı lot). max_value: kademe bütçesi (TL); risk_mult: rejim risk çarpanı."""
    fill = price * (1 + settings.sim_slippage_pct / 100)
    risk_per_share = fill - stop
    if risk_per_share <= 0 or fill <= 0:
        return 0
    eff_risk_pct = settings.sim_risk_per_trade_pct * max(0.1, min(1.5, risk_mult))
    by_risk = math.floor(equity * eff_risk_pct / 100 / risk_per_share)
    by_cap = math.floor(equity * settings.sim_max_position_pct / 100 / fill)
    by_cash = math.floor(cash / (fill * (1 + settings.sim_commission_pct / 100)))
    by_budget = math.floor(max_value / (fill * (1 + settings.sim_commission_pct / 100))) \
        if max_value is not None else by_cash
    return max(0, min(by_risk, by_cap, by_cash, by_budget))


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
            for sql in (
                "ALTER TABLE sim_runs ADD COLUMN last_yield_date TEXT",
                "ALTER TABLE sim_runs ADD COLUMN ppf_earned REAL DEFAULT 0",
                "ALTER TABLE sim_trades ADD COLUMN initial_stop REAL",
            ):
                try:
                    c.execute(sql)
                except sqlite3.OperationalError:
                    pass

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
            confidence: float | None = None, rationale: str = "", max_value: float | None = None) -> str | None:
        eq, _ = self.equity(run)
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
            qty = size_position(eq, cash, price, stop, max_value)
            if qty < 1:
                return self._event(c, run["id"], f"{ticker}: AL sinyali var ama pozisyon büyüklüğü 0 (nakit/risk sınırı)")
            fill = round(price * (1 + settings.sim_slippage_pct / 100), 4)
            comm = round(fill * qty * settings.sim_commission_pct / 100, 2)
            c.execute("UPDATE sim_runs SET cash = cash - ? WHERE id=?", (fill * qty + comm, run["id"]))
            c.execute(
                "INSERT INTO sim_trades (run_id, ticker, qty, entry_price, entry_date, stop_loss, initial_stop, target, entry_commission, "
                "confidence, rationale, status, last_bar_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run["id"], ticker, qty, fill, self.clock().isoformat(), stop, stop, target, comm, confidence, rationale,
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

    def _accrue_cash_yield(self, run: dict) -> str | None:
        """Boştaki nakde günlük Para Piyasası Fonu (PPF) getirisi işletir (0 ise devre dışı)."""
        rate = settings.sim_cash_daily_yield_pct
        if rate <= 0:
            return None
        today = self.clock()
        last_str = run.get("last_yield_date") or run["start_date"]
        last_d = date.fromisoformat(last_str)
        if today <= last_d:
            return None
        # İş günü sayısı kadar bileşik PPF getirisi
        bdays = sum(1 for i in range(1, (today - last_d).days + 1) if (last_d + timedelta(days=i)).weekday() < 5)
        if bdays <= 0:
            with self._conn() as c:
                c.execute("UPDATE sim_runs SET last_yield_date=? WHERE id=?", (today.isoformat(), run["id"]))
            return None
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
            if cash <= 0:
                c.execute("UPDATE sim_runs SET last_yield_date=? WHERE id=?", (today.isoformat(), run["id"]))
                return None
            gain = round(cash * (((1 + rate / 100) ** bdays) - 1), 2)
            c.execute(
                "UPDATE sim_runs SET cash = cash + ?, ppf_earned = COALESCE(ppf_earned, 0) + ?, last_yield_date = ? WHERE id=?",
                (gain, gain, today.isoformat(), run["id"]),
            )
            if gain >= 1.0:
                return self._event(c, run["id"], f"🏦 PPF nakit getirisi ({bdays} iş günü): +{gain:,.2f} TL")
        return None

    # ------------------------------------------------------------- günlük döngü
    def check_exits(self) -> list[str]:
        """Giriş gününden sonraki günlük barlarla stop/hedef/başabaş kontrolü + PPF nakit getirisi."""
        run = self.active_run()
        if not run:
            return []
        msgs = []
        if ppf_msg := self._accrue_cash_yield(run):
            msgs.append(ppf_msg)
        be_r = settings.sim_breakeven_r
        for t in self.open_trades(run["id"]):
            last_seen = date.fromisoformat(t["last_bar_date"])
            bars = self.feed.daily(t["ticker"], last_seen)
            bars = bars[[d > last_seen for d in bars.index]] if not bars.empty else bars
            exited = False
            cur_stop = float(t["stop_loss"])
            init_stop = float(t.get("initial_stop") or cur_stop)
            init_risk = max(0.01, float(t["entry_price"]) - init_stop)
            for d, b in bars.iterrows():
                if b["low"] <= cur_stop:
                    msgs.append(self.sell(t, min(b["open"], cur_stop), "stop-loss", d))
                    exited = True
                    break
                if b["high"] >= t["target"]:
                    msgs.append(self.sell(t, max(b["open"], t["target"]), "hedef", d))
                    exited = True
                    break
                # 1.5R başabaş (breakeven) stop kilidi — 0 LLM çağrısı
                if be_r > 0 and cur_stop < t["entry_price"] and b["high"] >= t["entry_price"] + be_r * init_risk:
                    cur_stop = round(t["entry_price"] * (1 + 2 * settings.sim_commission_pct / 100), 4)
                    with self._conn() as c:
                        c.execute("UPDATE sim_trades SET stop_loss=? WHERE id=?", (cur_stop, t["id"]))
                        msgs.append(self._event(c, run["id"], f"🛡️ {t['ticker']} {be_r}R görüldü → stop başabaşa çekildi ({cur_stop:.2f})"))
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

        # 1) Açık pozisyonlar: SAT -> kapat, AL -> stop'u yukarı çek (asla aşağı değil); güveni güncelle
        for t in self.open_trades(run["id"]):
            if settings.sim_skip_unchanged_reanalysis:
                p_now = self.feed.last(t["ticker"])
                # Fiyat stop'a tehlikeli yaklaşmadıysa ve girişten +%5'ten fazla uzaklaşmadıysa LLM kotası harcama
                if p_now is not None and t["stop_loss"] * 1.025 < p_now < t["entry_price"] * 1.05:
                    msgs.append(f"⚡ {t['ticker']}: anlamlı kırılım yok ({p_now:.2f}), LLM kotası korundu")
                    continue
            self.progress(f"🔁 Açık pozisyon {t['ticker']} yeniden değerlendiriliyor...")
            try:
                f = self.analyze_fn(t["ticker"])["final"]
            except Exception as exc:
                msgs.append(f"⚠️ {t['ticker']} yeniden analiz başarısız: {exc}")
                continue
            if f.decision == "SAT":
                p = self.feed.last(t["ticker"]) or t["entry_price"]
                msgs.append(self.sell(t, p, "SAT sinyali"))
                continue
            with self._conn() as c:
                if f.confidence > 0:  # LLM erişilemediyse (güven 0) eski güveni koru
                    c.execute("UPDATE sim_trades SET confidence=? WHERE id=?", (f.confidence, t["id"]))
                if f.decision == "AL" and f.trade_plan and f.trade_plan.stop_loss > t["stop_loss"]:
                    c.execute("UPDATE sim_trades SET stop_loss=? WHERE id=?", (f.trade_plan.stop_loss, t["id"]))
                    msgs.append(self._event(c, run["id"], f"⬆️ {t['ticker']} stop yükseltildi: "
                                                          f"{t['stop_loss']:.2f} → {f.trade_plan.stop_loss:.2f}"))

        # 2) Yeni adaylar (pozisyonlar dolu olsa da taranır: daha iyi adaya geçiş için)
        held = {t["ticker"] for t in self.open_trades(run["id"])}
        self.progress("🔍 BIST100 taranıyor (teknik puanlama, LLM yok)...")
        candidates = self.screen_fn(exclude=held)
        line = "🔍 Tarama adayları: " + (", ".join(f"{c['ticker']}({c['score']})" for c in candidates) or "yok")
        msgs.append(line)
        self.progress(line)
        buys = []
        for i, cand in enumerate(candidates, 1):
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
            buys.append((tk, f))
        # En güvenilir ve en yüksek risk/ödüllü aday parayı önce alır
        buys.sort(key=lambda x: (x[1].confidence, x[1].trade_plan.risk_reward), reverse=True)
        for tk, f in buys:
            msgs += self._enter(run, tk, f)
        self.snapshot()
        return msgs

    def _rotation_victim(self, run: dict, f, prices: dict[str, float]) -> dict | None:
        """Yeni adaydan belirgin biçimde zayıf olan en kötü pozisyon (bugün alınanlar hariç)."""
        today = self.clock().isoformat()
        new_rr = f.trade_plan.risk_reward
        weak = []
        for t in self.open_trades(run["id"]):
            if t["entry_date"] == today:
                continue
            p = prices.get(t["ticker"]) or t["entry_price"]
            conf, rr = (t["confidence"] or 0.0), remaining_rr(t, p)
            if (f.confidence >= conf + settings.sim_rotation_conf_margin
                    and new_rr >= rr * settings.sim_rotation_rr_factor and new_rr > rr):
                weak.append((conf, rr, t, p))
        if not weak:
            return None
        weak.sort(key=lambda x: (x[0], x[1]))
        conf, rr, t, p = weak[0]
        return {**t, "_price": p, "_rr": rr}

    def _room(self, run: dict, limit: float) -> tuple[float, float, int, dict[str, float]]:
        """(kademe limitine kadar yatırılabilecek ek tutar, özsermaye, açık pozisyon sayısı, fiyatlar)"""
        eq, prices = self.equity(run)
        with self._conn() as c:
            cash = c.execute("SELECT cash FROM sim_runs WHERE id=?", (run["id"],)).fetchone()["cash"]
        budget = max(0.0, min(cash, limit * eq - (eq - cash)))
        return budget, eq, len(self.open_trades(run["id"])), prices

    def _enter(self, run: dict, tk: str, f) -> list[str]:
        """Kademe limiti + gerekirse daha iyi adaya geçiş ile alım."""
        plan, conf = f.trade_plan, f.confidence
        tag = f"güven %{conf * 100:.0f}, R/Ö {plan.risk_reward}"
        price = self.feed.last(tk)
        if price is None:
            return [f"⚠️ {tk}: güncel fiyat yok"]
        chase_mult = 1.0 + settings.sim_entry_chase_pct / 100.0
        if price > plan.entry_high * chase_mult:
            return [f"⏸️ {tk}: fiyat {price:.2f} giriş bölgesinin ({plan.entry_high:.2f}) üstünde, kovalanmadı"]
        if price <= plan.stop_loss:
            return [f"⏸️ {tk}: fiyat stop seviyesinin altında, alım yok"]
        limit = tier_limit(conf, plan.risk_reward)
        if limit is None:
            return [f"⏸️ {tk}: AL ama hiçbir kademenin eşiğini karşılamıyor ({tag})"]

        msgs: list[str] = []
        budget, eq, n_open, prices = self._room(run, limit)
        min_ticket = max(price * 1.01, eq * 0.02)  # anlamlı bir pozisyon: en az özsermayenin %2'si
        if budget < min_ticket or n_open >= settings.sim_max_positions:
            victim = self._rotation_victim(run, f, prices)
            if victim is None:
                why = ("pozisyon sayısı dolu" if n_open >= settings.sim_max_positions
                       else f"%{limit * 100:.0f} kademe limiti dolu")
                return [f"⏸️ {tk}: AL ({tag}) ama {why}; eldekilerden belirgin biçimde üstün değil, geçiş yok"]
            msgs.append(self.sell(victim, victim["_price"],
                                  f"daha iyi adaya geçiş → {tk} ({tag}; eski güven "
                                  f"%{(victim['confidence'] or 0) * 100:.0f}, kalan R/Ö {victim['_rr']:.2f})"))
            budget, eq, n_open, prices = self._room(run, limit)
        msg = self.buy(run, tk, price, plan.stop_loss, plan.target, conf, f.rationale[:300], max_value=budget)
        if msg and msg.startswith("🟢"):
            msg += f" [kademe ≤%{limit * 100:.0f}, {tag}]"
        msgs.append(msg)
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
            row_r = c.execute("SELECT cash, COALESCE(ppf_earned, 0) AS ppf FROM sim_runs WHERE id=?", (run["id"],)).fetchone()
            cash, ppf_earned = row_r["cash"], row_r["ppf"]
        lines.append(f"Nakit: {cash:,.0f} TL" + (f" (PPF katkısı: +{ppf_earned:,.0f} TL)" if ppf_earned >= 1 else ""))

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
