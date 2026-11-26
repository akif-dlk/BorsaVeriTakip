"""Telegram arayüzü (tamamen async). Analiz ayrı thread'de koşar; bot bu sırada donmaz.

Güvenlik: yalnızca ALLOWED_TELEGRAM_USER_ID listesindeki kullanıcılara yanıt verilir,
diğer tüm güncellemeler sessizce yok sayılır.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from config.settings import VERI_YOK, settings
from graph.report import EMOJI, format_report
from graph.workflow import default_data_provider, run_analysis
from memory.db import PredictionStore
from tools import market_data, news_sentiment

log = logging.getLogger(__name__)
TG_LIMIT = 4000
MAX_CONCURRENT_ANALYSES = 2


@dataclass
class Services:
    llm_factory: Callable[[], Any]
    store: PredictionStore
    data_provider: Callable[[str], dict] = default_data_provider
    allowed_ids: set[int] = field(default_factory=set)
    watchlist: list[str] = field(default_factory=lambda: list(settings.watchlist))
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_ANALYSES))
    trader: Any = None  # simulation.paper_trader.PaperTrader
    sim_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def analyze_and_save(self, ticker: str) -> dict:
        """Senkron: simülasyon döngüsü (thread içinde) için analiz + kayıt."""
        state = run_analysis(ticker, self.llm_factory(), self.data_provider)
        self.store.save_prediction(state["final"], state)
        return state


def make_trader(svc: Services, db_path: str | None = None):
    from simulation.paper_trader import PaperTrader
    from tools.screener import screen

    return PaperTrader(db_path or svc.store.db_path, analyze_fn=svc.analyze_and_save, screen_fn=screen)


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def is_allowed(update: Update, svc: Services) -> bool:
    user = update.effective_user
    return bool(user and user.id in svc.allowed_ids)


def authorized(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update, _svc(context)):
            uid = update.effective_user.id if update.effective_user else None
            log.info("Yetkisiz erişim reddedildi (user_id=%s)", uid)
            return  # sessiz ret
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


async def _send_long(bot, chat_id: int, text: str) -> None:
    for part in split_message(text):
        await bot.send_message(chat_id=chat_id, text=part)


# ---------------- komutlar ----------------
HELP = (
    "🤖 Borsa Ajansı\n"
    "/analiz <HİSSE> — 3 kademeli tam analiz (ör. /analiz THYAO.IS)\n"
    "/gundem — günün piyasa/KAP özeti\n"
    "/gecmis — son tahminler ve başarı oranı\n"
    "/veri_guncelle — BIST100 + makro yerel veri ambarını güncelle (0 LLM)\n"
    "/backtest [gün] — yerel veri setinde 3 yıllık strateji testi (0 LLM)\n"
    "\n💼 Sanal portföy (gerçek para yok)\n"
    "/sim_baslat [tutar] [gün] — simülasyonu başlat (varsayılan 10000 TL, 14 gün)\n"
    "/portfoy — portföy durumu, K/Z ve BIST100 kıyası\n"
    "/sim_islem — tarama + alım/satım döngüsünü şimdi çalıştır\n"
    "/sim_log — son simülasyon olayları\n"
    "/sim_durdur — simülasyonu bitir, pozisyonları kapat\n"
)


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP)


async def analyze_ticker(svc: Services, ticker: str, progress_cb: Callable[[str], None] | None = None) -> dict:
    """Analizi thread'de çalıştırır (event loop'u bloklamaz), sonucu kaydeder."""
    async with svc.semaphore:
        state = await asyncio.to_thread(run_analysis, ticker, svc.llm_factory(), svc.data_provider, progress_cb)
    await asyncio.to_thread(svc.store.save_prediction, state["final"], state)
    return state


@authorized
async def cmd_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Kullanım: /analiz THYAO.IS")
        return
    ticker = market_data.normalize_ticker(context.args[0])
    status = await msg.reply_text(f"⏳ {ticker} analizi başlıyor...")
    loop = asyncio.get_running_loop()
    log_lines: list[str] = []

    async def _edit(text: str) -> None:
        try:
            await status.edit_text(text)
        except BadRequest as exc:  # "message is not modified" vb.
            log.debug("edit atlandı: %s", exc)

    def progress(step: str) -> None:  # worker thread'den çağrılır
        log_lines.append(step)
        text = f"⏳ {ticker}\n" + "\n".join(log_lines[-8:])
        asyncio.run_coroutine_threadsafe(_edit(text), loop)

    try:
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        state = await analyze_ticker(svc, ticker, progress)
    except Exception as exc:
        log.exception("Analiz hatası")
        await _edit(f"❌ {ticker} analizi başarısız: {exc}")
        return
    f = state["final"]
    await _edit(f"✅ {ticker} tamamlandı — {EMOJI[f.decision]} {f.decision} (güven %{f.confidence * 100:.0f})")
    await _send_long(context.bot, msg.chat_id, format_report(state))


def build_gundem_text() -> str:
    lines = ["📰 Günün Piyasa Özeti", ""]
    for sym, label in (("XU100.IS", "BIST 100"), ("USDTRY=X", "USD/TRY"), ("GC=F", "Ons Altın")):
        ind = market_data.compute_indicators(market_data.fetch_price_history(sym, "1mo"))
        if ind.get("status") == "ok":
            lines.append(f"• {label}: {ind['close']} ({ind['change_1d_pct']:+}% gün, {ind['change_5d_pct']:+}% hafta)"
                         if isinstance(ind["change_5d_pct"], float) else f"• {label}: {ind['close']}")
        else:
            lines.append(f"• {label}: {VERI_YOK}")
    macro = news_sentiment.get_macro_news(limit=3)
    kap = news_sentiment.fetch_news("KAP bildirimi", limit=5)
    lines += ["", "🏛️ Makro"]
    if macro["status"] != "ok":
        lines.append(f"• {VERI_YOK}")
    else:
        for q, items in macro["macro"].items():
            if isinstance(items, list):
                lines += [f"• {i['title']}" for i in items]
    lines += ["", "📢 KAP / Şirket gündemi"]
    lines += [f"• {i['title']}" for i in kap] if kap else [f"• {VERI_YOK}"]
    return "\n".join(lines)


@authorized
async def cmd_gundem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.effective_message.reply_text("⏳ Gündem toplanıyor...")
    text = await asyncio.to_thread(build_gundem_text)
    await status.delete()
    await _send_long(context.bot, update.effective_message.chat_id, text)


def build_gecmis_text(store: PredictionStore, limit: int = 10) -> str:
    rows = store.recent(limit)
    if not rows:
        return "Henüz kayıtlı tahmin yok."
    lines = ["📜 Son Tahminler", ""]
    for r in rows:
        ev = r["evaluations"]
        perf = " ".join(
            f"{h}g:{ev[h]['return_pct']:+.1f}%{'✅' if ev[h]['success'] == 1 else '❌' if ev[h]['success'] == 0 else ''}"
            for h in (5, 10) if h in ev) or "değerlendirme bekleniyor"
        lines.append(f"{EMOJI.get(r['decision'], '')} #{r['id']} {r['ticker']} {r['data_date']} {r['decision']} "
                     f"(%{r['confidence'] * 100:.0f}) — {perf}")
    lines += ["", "📊 Başarı oranı (AL/SAT)"]
    for h, s in store.stats().items():
        rate = f"%{s['success_rate'] * 100:.0f}" if s["success_rate"] is not None else "-"
        alpha_r = f", alfa başarısı %{s['alpha_rate'] * 100:.0f}" if s.get("alpha_rate") is not None else ""
        avg = f", ort. getiri {s['avg_return_pct']:+.2f}%" if s["avg_return_pct"] is not None else ""
        lines.append(f"• {h} işlem günü: {rate}{alpha_r} ({s['n']} tahmin{avg})")
    return "\n".join(lines)


@authorized
async def cmd_gecmis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    await asyncio.to_thread(svc.store.evaluate_due)
    await _send_long(context.bot, update.effective_message.chat_id, await asyncio.to_thread(build_gecmis_text, svc.store))


# ---------------- zamanlanmış rapor ----------------
async def morning_report(app: Application) -> None:
    svc: Services = app.bot_data["services"]
    if not svc.allowed_ids:
        log.warning("Sabah raporu: ALLOWED_TELEGRAM_USER_ID boş, gönderilmedi")
        return
    lines = ["🌅 Seans Öncesi İzleme Listesi Raporu", ""]
    for ticker in svc.watchlist:
        try:
            st = await analyze_ticker(svc, ticker)
            f = st["final"]
            extra = (f" | giriş {f.trade_plan.entry_low}-{f.trade_plan.entry_high}, stop {f.trade_plan.stop_loss}, "
                     f"hedef {f.trade_plan.target}, R/Ö {f.trade_plan.risk_reward}") if f.trade_plan else ""
            lines.append(f"{EMOJI[f.decision]} {f.ticker}: {f.decision} (%{f.confidence * 100:.0f}){extra}")
        except Exception as exc:
            log.exception("Sabah raporu %s", ticker)
            lines.append(f"❌ {ticker}: analiz başarısız ({exc})")
    lines += ["", "Detay için: /analiz <HİSSE>", "ℹ️ Yatırım tavsiyesi değildir."]
    text = "\n".join(lines)
    for uid in svc.allowed_ids:
        await _send_long(app.bot, uid, text)


# ---------------- sanal portföy ----------------
async def run_sim_cycle(app_bot, svc: Services, chat_ids: list[int], full: bool = True) -> None:
    """full=True: tarama + LLM analiz + emir; False: yalnızca stop/hedef kontrolü + rapor."""
    if svc.trader is None or not svc.trader.active_run() or svc.sim_lock.locked():
        return
    async with svc.sim_lock:
        loop = asyncio.get_running_loop()

        def progress(text: str) -> None:  # işçi thread'inden güvenli gönderim
            for cid in chat_ids:
                asyncio.run_coroutine_threadsafe(app_bot.send_message(chat_id=cid, text=text), loop)

        if full:
            svc.trader.progress = progress
        try:
            msgs = await asyncio.to_thread(svc.trader.trade_cycle if full else svc.trader.check_exits)
            if not full:
                await asyncio.to_thread(svc.trader.snapshot)
        except Exception as exc:
            log.exception("Simülasyon döngüsü hatası")
            msgs = [f"❌ Simülasyon döngüsü hatası: {exc}"]
        finally:
            svc.trader.progress = lambda text: None
        report = await asyncio.to_thread(svc.trader.report)
    title = "🤖 İşlem döngüsü" if full else "🌆 Gün sonu kontrolü"
    body = "\n".join(msgs) if msgs else "Yeni işlem yok."
    for cid in chat_ids:
        await _send_long(app_bot, cid, f"{title}\n{body}\n\n{report}")


def parse_sim_args(args: list[str]) -> tuple[float, int]:
    cash, days = settings.sim_initial_cash, settings.sim_days
    if args:
        cash = float(args[0].replace(".", "").replace(",", "."))
    if len(args) > 1:
        days = int(args[1])
    if not (1000 <= cash <= 100_000_000) or not (1 <= days <= 365):
        raise ValueError("tutar 1.000–100.000.000, gün 1–365 olmalı")
    return cash, days


@authorized
async def cmd_sim_baslat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc, msg = _svc(context), update.effective_message
    try:
        cash, days = parse_sim_args(context.args or [])
        run = await asyncio.to_thread(svc.trader.start, cash, days)
    except (ValueError, RuntimeError) as exc:
        await msg.reply_text(f"⚠️ {exc}\nKullanım: /sim_baslat 10000 14")
        return
    await msg.reply_text(
        f"💼 Simülasyon #{run['id']} başladı: {cash:,.0f} TL, {days} gün ({run['start_date']} → {run['end_date']}).\n"
        f"Şimdi ilk tarama ve analiz yapılıyor (birkaç dakika sürebilir)...\n"
        f"Otomatik: hafta içi {settings.sim_trade_hour:02d}:{settings.sim_trade_minute:02d} işlem döngüsü, "
        f"{settings.sim_eod_hour:02d}:{settings.sim_eod_minute:02d} gün sonu raporu.")
    context.application.create_task(run_sim_cycle(context.bot, svc, [msg.chat_id], full=True))


@authorized
async def cmd_portfoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    await _send_long(context.bot, update.effective_message.chat_id, await asyncio.to_thread(svc.trader.report))


@authorized
async def cmd_sim_islem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc, msg = _svc(context), update.effective_message
    if not svc.trader.active_run():
        await msg.reply_text("Aktif simülasyon yok. /sim_baslat ile başlat.")
        return
    if svc.sim_lock.locked():
        await msg.reply_text("Bir işlem döngüsü zaten çalışıyor, bitince rapor gelecek.")
        return
    await msg.reply_text("⏳ Tarama ve analiz başladı, sonuç birkaç dakika içinde gelecek...")
    context.application.create_task(run_sim_cycle(context.bot, svc, [msg.chat_id], full=True))


@authorized
async def cmd_sim_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    events = await asyncio.to_thread(svc.trader.recent_events, 20)
    await _send_long(context.bot, update.effective_message.chat_id,
                     "🧾 Son olaylar\n" + ("\n".join(events) if events else "Kayıt yok."))


@authorized
async def cmd_sim_durdur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    async with svc.sim_lock:
        run = await asyncio.to_thread(svc.trader.stop)
    if not run:
        await update.effective_message.reply_text("Aktif simülasyon yok.")
        return
    await _send_long(context.bot, update.effective_message.chat_id,
                     "⏹️ Simülasyon durduruldu.\n\n" + await asyncio.to_thread(svc.trader.report, run["id"]))


@authorized
async def cmd_veri_guncelle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from tools.data_warehouse import get_warehouse

    msg = update.effective_message
    status = await msg.reply_text("📦 Yerel BIST100 + makro veri ambarı güncelleniyor (0 LLM çağrısı)...")
    try:
        res = await asyncio.to_thread(get_warehouse().sync_market_data, years=3.0)
        await status.edit_text(
            f"✅ Veri seti güncellendi ({res['symbols']} sembol, {res['total_rows']:,} bar, "
            f"{res['min_date']} → {res['max_date']})."
        )
    except Exception as exc:
        log.exception("Veri güncelleme hatası")
        await status.edit_text(f"❌ Veri güncelleme hatası: {exc}")


@authorized
async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from simulation.backtest import format_backtest_report, run_backtest

    msg = update.effective_message
    days = int(context.args[0]) if (context.args and context.args[0].isdigit()) else 750
    status = await msg.reply_text(f"⏳ Son {days} işlem günü için 0-LLM backtest çalışıyor...")
    try:
        res = await asyncio.to_thread(run_backtest, None, days)
        await status.delete()
        await _send_long(context.bot, msg.chat_id, format_backtest_report(res))
    except Exception as exc:
        log.exception("Backtest hatası")
        await status.edit_text(f"❌ Backtest başarısız: {exc}")


async def data_sync_job(app: Application) -> None:
    """Seans kapanışı sonrası sessizce yerel veri ambarını günceller (0 LLM)."""
    from tools.data_warehouse import get_warehouse

    try:
        res = await asyncio.to_thread(get_warehouse().sync_market_data, years=3.0)
        log.info("Günlük veri botu tamamlandı: %s satır güncellendi", res.get("rows_upserted"))
    except Exception as exc:
        log.warning("Günlük veri botu hatası: %s", exc)


async def sim_trade_job(app: Application) -> None:
    svc: Services = app.bot_data["services"]
    await run_sim_cycle(app.bot, svc, list(svc.allowed_ids), full=True)


async def sim_eod_job(app: Application) -> None:
    svc: Services = app.bot_data["services"]
    await run_sim_cycle(app.bot, svc, list(svc.allowed_ids), full=False)


def setup_scheduler(app: Application):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    if settings.morning_report_enabled:
        sched.add_job(morning_report, "cron", day_of_week="mon-fri", hour=settings.report_hour,
                      minute=settings.report_minute, args=[app], id="morning_report", max_instances=1,
                      coalesce=True, misfire_grace_time=1800)
    if settings.data_sync_enabled:
        sched.add_job(data_sync_job, "cron", day_of_week="mon-fri", hour=settings.data_sync_hour,
                      minute=settings.data_sync_minute, args=[app], id="data_sync", max_instances=1,
                      coalesce=True, misfire_grace_time=3600)
    sched.add_job(sim_trade_job, "cron", day_of_week="mon-fri", hour=settings.sim_trade_hour,
                  minute=settings.sim_trade_minute, args=[app], id="sim_trade", max_instances=1,
                  coalesce=True, misfire_grace_time=1800)
    sched.add_job(sim_eod_job, "cron", day_of_week="mon-fri", hour=settings.sim_eod_hour,
                  minute=settings.sim_eod_minute, args=[app], id="sim_eod", max_instances=1,
                  coalesce=True, misfire_grace_time=3600)
    sched.start()
    return sched


# ---------------- uygulama ----------------
def build_application(services: Services, token: str) -> Application:
    async def _post_init(app: Application) -> None:
        sched = setup_scheduler(app)
        app.bot_data["scheduler"] = sched
        for job in sched.get_jobs():
            log.info("Zamanlanmış görev: %s -> sonraki çalışma %s", job.id, job.next_run_time)
        try:
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("analiz", "Hisse analizi: /analiz THYAO"),
                BotCommand("gundem", "Günün piyasa özeti"),
                BotCommand("gecmis", "Son tahminler ve başarı oranı"),
                BotCommand("veri_guncelle", "3 yıllık yerel veri setini güncelle (0 LLM)"),
                BotCommand("backtest", "3 yıllık strateji backtesti (0 LLM)"),
                BotCommand("sim_baslat", "Sanal portföy başlat: /sim_baslat 10000 14"),
                BotCommand("portfoy", "Sanal portföy durumu"),
                BotCommand("sim_islem", "Tarama + işlem döngüsünü şimdi çalıştır"),
                BotCommand("sim_log", "Son simülasyon olayları"),
                BotCommand("sim_durdur", "Simülasyonu bitir"),
                BotCommand("yardim", "Komut listesi"),
            ])
        except Exception as exc:  # menü kaydı kritik değil
            log.warning("Komut menüsü kaydedilemedi: %s", exc)

    async def _post_shutdown(app: Application) -> None:
        sched = app.bot_data.get("scheduler")
        if sched:
            sched.shutdown(wait=False)

    app = (ApplicationBuilder().token(token).concurrent_updates(True)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    app.bot_data["services"] = services
    app.add_handler(CommandHandler(["start", "yardim", "help"], cmd_start))
    app.add_handler(CommandHandler("analiz", cmd_analiz))
    app.add_handler(CommandHandler("gundem", cmd_gundem))
    app.add_handler(CommandHandler("gecmis", cmd_gecmis))
    app.add_handler(CommandHandler("veri_guncelle", cmd_veri_guncelle))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("sim_baslat", cmd_sim_baslat))
    app.add_handler(CommandHandler(["portfoy", "sim_rapor"], cmd_portfoy))
    app.add_handler(CommandHandler("sim_islem", cmd_sim_islem))
    app.add_handler(CommandHandler("sim_log", cmd_sim_log))
    app.add_handler(CommandHandler("sim_durdur", cmd_sim_durdur))
    return app


def run_bot() -> None:
    from agents.llm import get_llm

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN .env içinde tanımlı değil")
    if not settings.allowed_telegram_user_ids:
        log.warning("ALLOWED_TELEGRAM_USER_ID boş: bot TÜM mesajları sessizce reddedecek")
    services = Services(llm_factory=get_llm, store=PredictionStore(),
                        allowed_ids=set(settings.allowed_telegram_user_ids))
    services.trader = make_trader(services)
    app = build_application(services, settings.telegram_bot_token)
    log.info("Bot başlatılıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
