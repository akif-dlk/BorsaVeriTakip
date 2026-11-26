"""Yerel Veri Ambarı ve Veri Çekme Botu (0 LLM Çağrısı).

Amaç:
- BIST100 hisseleri + 13 küresel/yerel makro enstrüman için son 2-3 yıllık günlük OHLCV
  verisini toplu (`yf.download`) çekip yerel SQLite ambarına (`data/market_warehouse.db`)
  ve taşınabilir veri setine (`data/bist100_3y_daily.csv`) kaydetmek.
- Günlük artımlı (incremental) güncelleme yapmak: yalnızca eksik son günler indirilir (~2-3 sn).
- Ham KAP bildirimlerini LLM kullanmadan önbelleğe almak.
- Analiz, tarama ve backtest modüllerine milisaniyeler içinde çevrimdışı/yerel veri sunmak;
  böylece LLM API'leri veri toplamakla değil, yalnızca karar/analiz (düşünme) ile uğraşır.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from config.settings import settings

log = logging.getLogger(__name__)

WAREHOUSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv_daily(date);

CREATE TABLE IF NOT EXISTS kap_raw_cache (
    symbol TEXT NOT NULL,
    disclosure_index INTEGER NOT NULL,
    pub_date TEXT NOT NULL,
    cls TEXT,
    title TEXT,
    summary TEXT,
    body_text TEXT,
    attachments INTEGER DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, disclosure_index)
);
CREATE INDEX IF NOT EXISTS idx_kap_symbol_date ON kap_raw_cache(symbol, pub_date);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbols_count INTEGER NOT NULL,
    rows_upserted INTEGER NOT NULL,
    min_date TEXT,
    max_date TEXT,
    notes TEXT
);
"""

PERIOD_DAYS: dict[str, int] = {
    "5d": 10,
    "1mo": 38,
    "3mo": 105,
    "6mo": 195,
    "1y": 375,
    "2y": 745,
    "3y": 1110,
    "5y": 1850,
}


def default_universe_with_macro() -> list[str]:
    from tools.market_context import INSTRUMENTS
    from tools.screener import BIST100

    bist = [f"{s}.IS" if "." not in s else s for s in BIST100]
    macro = list(INSTRUMENTS.keys())
    return list(dict.fromkeys(macro + bist))


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.rename(columns=str.lower)
    needed = ["open", "high", "low", "close"]
    if not all(c in out.columns for c in needed):
        return pd.DataFrame()
    if "volume" not in out.columns:
        out = out.assign(volume=0.0)
    out = out[["open", "high", "low", "close", "volume"]].dropna(subset=["close"]).copy()
    if out.empty:
        return pd.DataFrame()
    out["volume"] = out["volume"].fillna(0.0)
    out["open"] = out["open"].fillna(out["close"])
    out["high"] = out["high"].fillna(out["close"])
    out["low"] = out["low"].fillna(out["close"])
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


class DataWarehouse:
    def __init__(self, db_path: str | None = None, csv_path: str | None = None):
        self.db_path = Path(db_path or settings.warehouse_db_path)
        self.csv_path = Path(csv_path or settings.warehouse_csv_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL;")
            c.executescript(WAREHOUSE_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------------ OHLCV yazma / senkronizasyon
    def upsert_ohlcv(self, ticker: str, df: pd.DataFrame) -> int:
        clean = _normalize_df(df)
        if clean.empty:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                ticker,
                idx.strftime("%Y-%m-%d"),
                round(float(r["open"]), 4),
                round(float(r["high"]), 4),
                round(float(r["low"]), 4),
                round(float(r["close"]), 4),
                round(float(r["volume"]), 2),
                now,
            )
            for idx, r in clean.iterrows()
        ]
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO ohlcv_daily "
                "(ticker, date, open, high, low, close, volume, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def symbol_coverage(self) -> dict[str, tuple[int, str, str]]:
        """ticker -> (satır_sayısı, ilk_tarih, son_tarih)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT ticker, COUNT(*) AS n, MIN(date) AS d0, MAX(date) AS d1 FROM ohlcv_daily GROUP BY ticker"
            ).fetchall()
        return {r["ticker"]: (int(r["n"]), r["d0"], r["d1"]) for r in rows}

    def sync_market_data(
        self,
        tickers: list[str] | None = None,
        years: float = 3.0,
        batch_size: int = 30,
        force_full: bool = False,
        export_csv: bool = True,
        downloader: Callable[..., dict[str, pd.DataFrame]] | None = None,
        today: date | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """BIST100 + makro verilerini toplu indirir, SQLite ve CSV veri setini günceller (0 LLM)."""
        say = progress or (lambda m: log.info(m))
        today = today or date.today()
        symbols = tickers or default_universe_with_macro()
        cov = {} if force_full else self.symbol_coverage()
        full_start = today - timedelta(days=int(years * 366))

        need_full: list[str] = []
        need_inc: list[str] = []
        inc_dates: list[date] = []
        for s in symbols:
            info = cov.get(s)
            if not info or info[0] < 180:
                need_full.append(s)
            else:
                last_d = date.fromisoformat(info[2])
                if last_d <= today:
                    need_inc.append(s)
                    inc_dates.append(last_d)

        dl = downloader or _yfinance_batch_download
        total_rows = 0
        updated_symbols = 0

        for label, group, start_d in (
            (f"{years:.0f} yıllık tam geçmiş", need_full, full_start),
            ("günlük artımlı fark", need_inc, (min(inc_dates) - timedelta(days=5)) if inc_dates else today),
        ):
            if not group:
                continue
            for i in range(0, len(group), batch_size):
                chunk = group[i : i + batch_size]
                say(f"📦 Veri botu ({label}): {i + 1}-{i + len(chunk)}/{len(group)} sembol çekiliyor ({start_d} → {today})...")
                fetched = dl(chunk, start=start_d)
                for sym, df in fetched.items():
                    n = self.upsert_ohlcv(sym, df)
                    if n > 0:
                        total_rows += n
                        updated_symbols += 1

        stats = self.stats()
        with self._conn() as c:
            c.execute(
                "INSERT INTO sync_runs (ts, symbols_count, rows_upserted, min_date, max_date, notes) VALUES (?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    updated_symbols,
                    total_rows,
                    stats["min_date"],
                    stats["max_date"],
                    f"years={years}, full={len(need_full)}, inc={len(need_inc)}",
                ),
            )

        csv_file = None
        if export_csv and stats["total_rows"] > 0:
            csv_file = str(self.export_csv())
            say(f"💾 Taşınabilir veri seti güncellendi: {csv_file} ({stats['total_rows']:,} satır)")

        return {**stats, "updated_symbols": updated_symbols, "rows_upserted": total_rows, "csv_path": csv_file}

    def export_csv(self, path: str | Path | None = None) -> Path:
        target = Path(path or self.csv_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            df = pd.read_sql_query(
                "SELECT date, ticker, open, high, low, close, volume FROM ohlcv_daily ORDER BY date, ticker",
                c,
            )
        df.to_csv(target, index=False)
        return target

    # ------------------------------------------------------------------ OHLCV okuma
    def get_ohlcv(
        self,
        ticker: str,
        period: str = "1y",
        min_rows: int = 5,
        max_staleness_days: int = 5,
        today: date | None = None,
    ) -> pd.DataFrame:
        """Yerel ambardan OHLCV döndürür. Veri eski veya eksikse boş DataFrame döner (canlı yedeğe düşmesi için)."""
        days = PERIOD_DAYS.get(period, 375)
        ref = today or date.today()
        start_str = (ref - timedelta(days=days)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
                "WHERE ticker = ? AND date >= ? ORDER BY date ASC",
                (ticker, start_str),
            ).fetchall()
        if len(rows) < min_rows:
            return pd.DataFrame()
        last_d = date.fromisoformat(rows[-1]["date"])
        if (ref - last_d).days > max_staleness_days:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df.index = pd.to_datetime(df.pop("date"))
        return df[["open", "high", "low", "close", "volume"]]

    def load_history_map(
        self,
        tickers: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Backtest ve toplu analiz için tüm sembolleri tek SQL sorgusuyla belleğe yükler."""
        clauses, params = [], []
        if start:
            clauses.append("date >= ?")
            params.append(start)
        if end:
            clauses.append("date <= ?")
            params.append(end)
        if tickers:
            ph = ",".join("?" for _ in tickers)
            clauses.append(f"ticker IN ({ph})")
            params.extend(tickers)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            big = pd.read_sql_query(
                f"SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily {where} ORDER BY ticker, date",
                c,
                params=params,
            )
        if big.empty:
            return {}
        big["date"] = pd.to_datetime(big["date"])
        out: dict[str, pd.DataFrame] = {}
        for tk, grp in big.groupby("ticker", sort=False):
            g = grp.set_index("date")[["open", "high", "low", "close", "volume"]].copy()
            out[str(tk)] = g
        return out

    # ------------------------------------------------------------------ KAP ham bildirim botu (0 LLM)
    def sync_kap_disclosures(
        self,
        tickers: list[str] | None = None,
        lookback_days: int = 30,
        max_details_per_symbol: int = 3,
        get: Callable | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, int]:
        """KAP bildirimlerini LLM kullanmadan çeker ve yerel veritabanına yazar."""
        from tools import kap

        say = progress or (lambda m: log.info(m))
        http_get = get or kap.http_get
        symbols = [kap.base_symbol(t) for t in (tickers or settings.watchlist)]
        cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        now_iso = datetime.now(timezone.utc).isoformat()
        saved, checked = 0, 0

        for sym in symbols:
            checked += 1
            say(f"📢 KAP botu ({checked}/{len(symbols)}): {sym} bildirimleri kontrol ediliyor...")
            discs = kap.list_disclosures(sym, http_get)
            if not discs:
                continue
            recent = [d for d in discs if (d.get("date") or "")[:10] >= cutoff]
            with self._conn() as c:
                existing = {
                    r[0]
                    for r in c.execute(
                        "SELECT disclosure_index FROM kap_raw_cache WHERE symbol = ?", (sym,)
                    ).fetchall()
                }
            det_fetched = 0
            for d in recent:
                idx = int(d["index"])
                if idx in existing:
                    continue
                body = ""
                if d.get("class") != "DG" and det_fetched < max_details_per_symbol:
                    det = kap.disclosure_detail(idx, http_get)
                    body = (det or {}).get("text") or ""
                    det_fetched += 1
                with self._conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO kap_raw_cache "
                        "(symbol, disclosure_index, pub_date, cls, title, summary, body_text, attachments, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            sym,
                            idx,
                            d.get("date", ""),
                            d.get("class", ""),
                            d.get("title", ""),
                            d.get("summary", ""),
                            body,
                            int(d.get("attachments") or 0),
                            now_iso,
                        ),
                    )
                saved += 1
        return {"symbols_checked": checked, "disclosures_saved": saved}

    def recent_kap_catalysts(self, days: int = 7, today: date | None = None) -> dict[str, list[dict[str, Any]]]:
        """Son N günde yerel KAP önbelleğinde bildirim bulunan sembolleri döndürür."""
        cutoff = ((today or date.today()) - timedelta(days=days)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT symbol, disclosure_index, pub_date, cls, title, summary, body_text "
                "FROM kap_raw_cache WHERE substr(pub_date, 1, 10) >= ? ORDER BY pub_date DESC",
                (cutoff,),
            ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(f"{r['symbol']}.IS", []).append(dict(r))
        return out

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) AS rows, COUNT(DISTINCT ticker) AS syms, MIN(date) AS d0, MAX(date) AS d1 FROM ohlcv_daily"
            ).fetchone()
            kap_n = c.execute("SELECT COUNT(*) FROM kap_raw_cache").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "csv_path": str(self.csv_path),
            "csv_exists": self.csv_path.exists(),
            "total_rows": int(r["rows"] or 0),
            "symbols": int(r["syms"] or 0),
            "min_date": r["d0"],
            "max_date": r["d1"],
            "kap_cached": int(kap_n or 0),
        }


def _yfinance_batch_download(symbols: list[str], start: date) -> dict[str, pd.DataFrame]:
    """yfinance ile çoklu sembolleri tek HTTP havuzunda hızlıca indirir."""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    if not symbols:
        return out
    try:
        raw = yf.download(
            tickers=symbols,
            start=start.isoformat(),
            interval="1d",
            auto_adjust=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as exc:
        log.warning("Toplu indirme hatası (%d sembol): %s", len(symbols), exc)
        raw = pd.DataFrame()

    if raw is not None and not raw.empty:
        if isinstance(raw.columns, pd.MultiIndex):
            lvl0 = set(raw.columns.get_level_values(0))
            for s in symbols:
                if s in lvl0:
                    sub = _normalize_df(raw[s])
                    if not sub.empty:
                        out[s] = sub
        elif len(symbols) == 1:
            sub = _normalize_df(raw)
            if not sub.empty:
                out[symbols[0]] = sub

    # Toplu indirmede eksik kalan semboller için tekil yedek deneme
    missing = [s for s in symbols if s not in out]
    for s in missing:
        try:
            df = yf.Ticker(s).history(start=start.isoformat(), interval="1d", auto_adjust=False)
            sub = _normalize_df(df)
            if not sub.empty:
                out[s] = sub
        except Exception as exc:
            log.debug("Tekil fiyat indirilemedi %s: %s", s, exc)
    return out


_DEFAULT_WH: DataWarehouse | None = None


def get_warehouse() -> DataWarehouse:
    global _DEFAULT_WH
    if _DEFAULT_WH is None or str(_DEFAULT_WH.db_path) != str(settings.warehouse_db_path):
        _DEFAULT_WH = DataWarehouse(settings.warehouse_db_path, settings.warehouse_csv_path)
    return _DEFAULT_WH
