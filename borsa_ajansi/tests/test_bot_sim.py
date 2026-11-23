import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.db import PredictionStore
from simulation.paper_trader import PaperTrader
from telegram_bot import bot
from tests.fakes import FakeLLM, make_bundle
from tests.test_simulation import FakeFeed
from tests.test_workflow import Script


@pytest.fixture
def svc(tmp_path, ohlcv):
    def prov(t):
        b = make_bundle(ohlcv, t)
        ind = b["market"]["indicators"]
        ind["levels"]["resistance_60d"] = round(ind["close"] + 8 * ind["atr14"], 4)
        return b

    s = bot.Services(llm_factory=lambda: FakeLLM(Script()), store=PredictionStore(str(tmp_path / "b.db")),
                     data_provider=prov, allowed_ids={42})
    feed = FakeFeed()
    close = make_bundle(ohlcv)["market"]["indicators"]["close"]
    feed.prices.update({"AAA.IS": close})
    s.trader = PaperTrader(str(tmp_path / "b.db"), feed=feed, analyze_fn=s.analyze_and_save,
                           screen_fn=lambda exclude=None: [{"ticker": "AAA.IS", "score": 60}],
                           clock=lambda: date(2026, 11, 23))
    return s


def _ctx(svc, args=None):
    tasks = []
    app = SimpleNamespace(bot_data={"services": svc}, create_task=lambda coro: tasks.append(coro))
    b = MagicMock()
    b.send_message = AsyncMock()
    return SimpleNamespace(application=app, args=args or [], bot=b), tasks


def _update(uid):
    msg = MagicMock()
    msg.chat_id = 555
    msg.reply_text = AsyncMock()
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=msg)


def test_parse_sim_args():
    assert bot.parse_sim_args([]) == (10000.0, 14)
    assert bot.parse_sim_args(["25.000", "7"]) == (25000.0, 7)
    with pytest.raises(ValueError):
        bot.parse_sim_args(["5", "14"])


async def test_unauthorized_cannot_touch_simulation(svc):
    upd = _update(1)
    ctx, tasks = _ctx(svc, ["10000"])
    for h in (bot.cmd_sim_baslat, bot.cmd_portfoy, bot.cmd_sim_islem, bot.cmd_sim_log, bot.cmd_sim_durdur):
        await h(upd, ctx)
    upd.effective_message.reply_text.assert_not_called()
    assert svc.trader.active_run() is None and not tasks


async def test_sim_start_runs_cycle_and_buys(svc):
    upd = _update(42)
    ctx, tasks = _ctx(svc, ["10000", "14"])
    await bot.cmd_sim_baslat(upd, ctx)
    assert "başladı" in upd.effective_message.reply_text.call_args.args[0]
    assert len(tasks) == 1
    await tasks[0]  # arka plan döngüsü
    sent = "\n".join(c.kwargs["text"] for c in ctx.bot.send_message.call_args_list)
    assert "ALIŞ AAA.IS" in sent and "Sanal Portföy" in sent
    # ikinci başlatma reddedilir
    upd2 = _update(42)
    await bot.cmd_sim_baslat(upd2, _ctx(svc)[0])
    assert "Zaten aktif" in upd2.effective_message.reply_text.call_args.args[0]


async def test_portfoy_and_stop(svc):
    svc.trader.start(10000, 14)
    upd = _update(42)
    ctx, _ = _ctx(svc)
    await bot.cmd_portfoy(upd, ctx)
    assert "10,000 TL" in ctx.bot.send_message.call_args.kwargs["text"]
    await bot.cmd_sim_durdur(upd, ctx)
    assert "durduruldu" in ctx.bot.send_message.call_args.kwargs["text"]
    assert svc.trader.active_run() is None


async def test_scheduled_jobs_send_to_allowed(svc):
    svc.trader.start(10000, 14)
    app = SimpleNamespace(bot_data={"services": svc}, bot=MagicMock())
    app.bot.send_message = AsyncMock()
    await bot.sim_eod_job(app)
    kw = app.bot.send_message.call_args.kwargs
    assert kw["chat_id"] == 42 and "Gün sonu" in kw["text"]


def test_scheduler_has_sim_jobs(svc):
    async def _run():
        sched = bot.setup_scheduler(SimpleNamespace(bot_data={"services": svc}))
        ids = {j.id for j in sched.get_jobs()}
        sched.shutdown(wait=False)
        return ids
    assert {"morning_report", "sim_trade", "sim_eod"} <= asyncio.run(_run())
