from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from memory.db import PredictionStore, judge
from telegram_bot import bot
from tests.fakes import FakeLLM, make_bundle
from tests.test_workflow import Script
from verification.schemas import FinalDecision, TradePlan


def _final(ticker="TEST.IS", decision="AL", d="2026-01-02", close=100.0):
    plan = TradePlan(entry_low=99, entry_high=100, stop_loss=96, target=110, risk_reward=2.5, basis="x") if decision != "BEKLE" else None
    return FinalDecision(ticker=ticker, data_date=d, decision=decision, llm_decision=decision, raw_confidence=0.8,
                         confidence=0.8, winning_side="boga", rationale="r", key_risks=[], trade_plan=plan, close=close)


# ---------------- memory ----------------
def test_store_save_recent_and_evaluate(tmp_path):
    s = PredictionStore(str(tmp_path / "m.db"))
    p1 = s.save_prediction(_final("AAA.IS", "AL"))
    p2 = s.save_prediction(_final("BBB.IS", "SAT"))
    s.save_prediction(_final("CCC.IS", "BEKLE"))
    assert [r["id"] for r in s.recent(2)] == [3, 2]

    base = date(2026, 1, 2)
    days = [base + timedelta(days=i) for i in range(1, 15)]

    def prices(ticker, start):
        # AAA yükseliyor (AL doğru), BBB de yükseliyor (SAT yanlış)
        return pd.Series([100 + i for i in range(1, 15)], index=days, dtype=float)

    added = s.evaluate_due(prices, today=base + timedelta(days=30))
    assert added == 6  # 3 tahmin x 2 vade
    assert s.evaluate_due(prices, today=base + timedelta(days=30)) == 0  # idempotent
    rec = {r["id"]: r for r in s.recent(10)}
    assert rec[p1]["evaluations"][5]["return_pct"] == pytest.approx(5.0)
    assert rec[p1]["evaluations"][10]["success"] == 1
    assert rec[p2]["evaluations"][5]["success"] == 0
    st = s.stats()
    assert st[5]["n"] == 2 and st[5]["success_rate"] == 0.5  # BEKLE başarı oranına dahil değil


def test_evaluate_waits_for_horizon(tmp_path):
    s = PredictionStore(str(tmp_path / "m.db"))
    s.save_prediction(_final())
    days = [date(2026, 1, 3) + timedelta(days=i) for i in range(3)]
    assert s.evaluate_due(lambda t, st: pd.Series([101.0, 102, 103], index=days), today=date(2026, 1, 10)) == 0


def test_judge():
    assert judge("AL", 1.0) == 1 and judge("AL", -1.0) == 0
    assert judge("SAT", -1.0) == 1 and judge("BEKLE", 5) is None


def test_verification_logs_saved(tmp_path, ohlcv):
    from graph.workflow import run_analysis
    s = PredictionStore(str(tmp_path / "m.db"))
    st = run_analysis("TEST.IS", FakeLLM(Script(liar_times={"teknik": 1})), data_provider=lambda t: make_bundle(ohlcv, t))
    pid = s.save_prediction(st["final"], st)
    logs = s.verification_logs(pid)
    stages = {l["stage"] for l in logs}
    assert {"fact_check", "debate_r1", "debate_r2", "risk_gate"} <= stages
    teknik = next(l for l in logs if l["stage"] == "fact_check" and l["analyst"] == "teknik")
    assert teknik["attempts"] == 2


# ---------------- telegram ----------------
def _ctx(svc, args=None):
    app = SimpleNamespace(bot_data={"services": svc})
    b = MagicMock()
    b.send_message = AsyncMock()
    b.send_chat_action = AsyncMock()
    return SimpleNamespace(application=app, args=args or [], bot=b)


def _update(user_id):
    status = MagicMock()
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    msg = MagicMock()
    msg.chat_id = 555
    msg.reply_text = AsyncMock(return_value=status)
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=msg), status


@pytest.fixture
def svc(tmp_path, ohlcv):
    def prov(t):
        b = make_bundle(ohlcv, t)
        ind = b["market"]["indicators"]
        ind["levels"]["resistance_60d"] = round(ind["close"] + 8 * ind["atr14"], 4)
        return b
    return bot.Services(llm_factory=lambda: FakeLLM(Script()), store=PredictionStore(str(tmp_path / "b.db")),
                        data_provider=prov, allowed_ids={42})


async def test_unauthorized_user_silently_ignored(svc):
    upd, _ = _update(999)
    ctx = _ctx(svc, ["THYAO"])
    for handler in (bot.cmd_start, bot.cmd_analiz, bot.cmd_gundem, bot.cmd_gecmis):
        await handler(upd, ctx)
    upd.effective_message.reply_text.assert_not_called()
    ctx.bot.send_message.assert_not_called()


async def test_analiz_requires_ticker(svc):
    upd, _ = _update(42)
    await bot.cmd_analiz(upd, _ctx(svc, []))
    assert "Kullanım" in upd.effective_message.reply_text.call_args.args[0]


async def test_analiz_full_flow_updates_status_and_sends_report(svc):
    upd, status = _update(42)
    ctx = _ctx(svc, ["test.is"])
    await bot.cmd_analiz(upd, ctx)
    edits = [c.args[0] for c in status.edit_text.call_args_list]
    assert any("tamamlandı" in e for e in edits)
    sent = "\n".join(c.kwargs["text"] for c in ctx.bot.send_message.call_args_list)
    assert "KARAR: AL" in sent
    assert svc.store.recent(1)[0]["ticker"] == "TEST.IS"


async def test_analiz_error_reported_not_crashing(svc):
    def boom():
        raise RuntimeError("ANTHROPIC_API_KEY yok")
    svc.llm_factory = boom
    upd, status = _update(42)
    await bot.cmd_analiz(upd, _ctx(svc, ["TEST.IS"]))
    assert "başarısız" in status.edit_text.call_args.args[0]


async def test_gecmis(svc, monkeypatch):
    svc.store.save_prediction(_final())
    monkeypatch.setattr(svc.store, "evaluate_due", lambda *a, **k: 0)
    upd, _ = _update(42)
    ctx = _ctx(svc)
    await bot.cmd_gecmis(upd, ctx)
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "TEST.IS" in text and "Başarı oranı" in text


async def test_morning_report_sends_to_allowed_users(svc):
    svc.watchlist = ["AAA.IS", "BBB.IS"]
    app = SimpleNamespace(bot_data={"services": svc}, bot=MagicMock())
    app.bot.send_message = AsyncMock()
    await bot.morning_report(app)
    kw = app.bot.send_message.call_args.kwargs
    assert kw["chat_id"] == 42
    assert "AAA.IS" in kw["text"] and "BBB.IS" in kw["text"]


def test_split_message():
    parts = bot.split_message("a" * 9000 + "\nb")
    assert all(len(p) <= bot.TG_LIMIT for p in parts) and "".join(parts).replace("\n", "") == "a" * 9000 + "b"


def test_scheduler_job_configured(svc):
    import asyncio

    async def _run():
        app = SimpleNamespace(bot_data={"services": svc})
        sched = bot.setup_scheduler(app)
        job = sched.get_job("morning_report")
        fields = {f.name: str(f) for f in job.trigger.fields}
        sched.shutdown(wait=False)
        return fields
    fields = asyncio.run(_run())
    assert fields["day_of_week"] == "mon-fri" and fields["hour"] == "9" and fields["minute"] == "30"
