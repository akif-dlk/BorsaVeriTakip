"""Giriş noktası.

  python main.py analiz THYAO.IS          # tek hisse uçtan uca CLI analizi
  python main.py veri-cek --years 3       # 3 yıllık BIST100+makro veri setini indir/güncelle (0 LLM)
  python main.py backtest --days 750      # yerel veri seti üzerinde 3 yıllık backtest (0 LLM)
  python main.py bot                      # Telegram botu + zamanlayıcı
"""
from __future__ import annotations

import argparse
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "yfinance", "peewee", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_analiz(ticker: str, save: bool = True) -> int:
    from agents.llm import get_llm
    from graph.report import format_report
    from graph.workflow import run_analysis

    state = run_analysis(ticker, get_llm(), progress=lambda m: print(m, flush=True))
    print("\n" + format_report(state))
    if save:
        from memory.db import PredictionStore
        pid = PredictionStore().save_prediction(state["final"], state)
        print(f"\n💾 Tahmin kaydedildi (id={pid})")
    return 0


def cmd_veri_cek(years: float = 3.0, force_full: bool = False, sync_kap: bool = False) -> int:
    from tools.data_warehouse import get_warehouse

    wh = get_warehouse()
    res = wh.sync_market_data(years=years, force_full=force_full, progress=lambda m: print(m, flush=True))
    print(
        f"\n✅ Veri ambarı hazır: {res['symbols']} sembol, {res['total_rows']:,} günlük bar "
        f"({res['min_date']} → {res['max_date']})\n"
        f"   SQLite: {res['db_path']}\n   CSV:    {res['csv_path']}"
    )
    if sync_kap:
        from tools.screener import BIST30

        kres = wh.sync_kap_disclosures(tickers=BIST30, progress=lambda m: print(m, flush=True))
        print(f"✅ KAP önbelleği: {kres['symbols_checked']} şirket tarandı, {kres['disclosures_saved']} yeni bildirim kaydedildi.")
    return 0


def cmd_backtest(days: int = 750, cash: float = 100_000.0, ppf: float = 0.10) -> int:
    from simulation.backtest import BacktestConfig, format_backtest_report, run_backtest

    cfg = BacktestConfig(initial_cash=cash, ppf_daily_pct=ppf)
    res = run_backtest(days=days, cfg=cfg)
    print("\n" + format_backtest_report(res))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _setup_logging()
    p = argparse.ArgumentParser(description="Çoklu ajanlı borsa analiz, veri ambarı ve backtest sistemi")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analiz", help="Tek hisseyi analiz et")
    a.add_argument("ticker")
    a.add_argument("--no-save", action="store_true")

    vc = sub.add_parser("veri-cek", help="3 yıllık BIST100 + makro veri setini indir/güncelle (0 LLM)")
    vc.add_argument("--years", type=float, default=3.0, help="Kaç yıllık geçmiş indirilsin (varsayılan: 3)")
    vc.add_argument("--full", action="store_true", help="Mevcut veriyi yok sayıp baştan indir")
    vc.add_argument("--kap", action="store_true", help="BIST30 son KAP bildirimlerini de önbelleğe çek")

    bt = sub.add_parser("backtest", help="Yerel 3 yıllık veri seti üzerinde deterministik backtest (0 LLM)")
    bt.add_argument("--days", type=int, default=750, help="Test edilecek işlem günü sayısı (750 ≈ 3 yıl)")
    bt.add_argument("--cash", type=float, default=100_000.0, help="Başlangıç sermayesi (TL)")
    bt.add_argument("--ppf", type=float, default=0.10, help="Boştaki nakit günlük PPF getirisi (%)")

    sub.add_parser("bot", help="Telegram botunu başlat")
    args = p.parse_args(argv)

    if args.cmd == "analiz":
        return cmd_analiz(args.ticker, save=not args.no_save)
    if args.cmd == "veri-cek":
        return cmd_veri_cek(years=args.years, force_full=args.full, sync_kap=args.kap)
    if args.cmd == "backtest":
        return cmd_backtest(days=args.days, cash=args.cash, ppf=args.ppf)
    if args.cmd == "bot":
        from telegram_bot.bot import run_bot
        run_bot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
