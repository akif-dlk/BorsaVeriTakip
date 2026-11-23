"""Giriş noktası.

  python main.py analiz THYAO.IS    # tek hisse uçtan uca CLI analizi
  python main.py bot                # Telegram botu + 09:30 zamanlayıcı
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


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _setup_logging()
    p = argparse.ArgumentParser(description="Çoklu ajanlı borsa analiz sistemi")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analiz", help="Tek hisseyi analiz et")
    a.add_argument("ticker")
    a.add_argument("--no-save", action="store_true")
    sub.add_parser("bot", help="Telegram botunu başlat")
    args = p.parse_args(argv)

    if args.cmd == "analiz":
        return cmd_analiz(args.ticker, save=not args.no_save)
    if args.cmd == "bot":
        from telegram_bot.bot import run_bot
        run_bot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
