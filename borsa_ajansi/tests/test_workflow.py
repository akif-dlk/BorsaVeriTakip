import pytest

import main as main_mod
from agents import risk_manager
from graph.workflow import run_analysis
from tests.fakes import FakeLLM, extract_analyst, extract_raw, make_bundle
from verification.fact_checker import flatten
from verification.schemas import AnalystReport, Argument, DebateTurn, NumericClaim, RiskJudgement


class Script:
    """Senaryolu sahte LLM davranışı."""

    def __init__(self, liar_times=None, decision="AL", confidence=0.85, conflict=False,
                 bull_hallucinates=True, bear_empty_rounds=()):
        self.liar_times = dict(liar_times or {})
        self.decision, self.confidence, self.conflict = decision, confidence, conflict
        self.bull_hallucinates = bull_hallucinates
        self.bear_empty_rounds = set(bear_empty_rounds)

    def __call__(self, schema, system, user):
        if schema is AnalystReport:
            name = extract_analyst(user)
            raw = flatten(extract_raw(user))
            field = {"teknik": "rsi14", "temel": "pe_ratio", "gundem": "news_count", "duygu": "rsi14"}[name]
            claims = []
            if isinstance(raw.get(field), (int, float)):
                val = raw[field]
                if self.liar_times.get(name, 0) > 0:
                    self.liar_times[name] -= 1
                    val = val + 25  # halüsinasyon
                claims = [NumericClaim(field=field, value=val)]
            sig = "olumsuz" if (self.conflict and name == "temel") else "olumlu"
            return AnalystReport(analyst=name, ticker="TEST.IS", data_date=raw.get("date", "veri_yok"), signal=sig,
                                 score=0.5, confidence=0.8, key_points=[f"{name} görüşü"], claims=claims,
                                 data_gaps=[], summary="özet")
        if schema is DebateTurn:
            rnd = int(user.split("Tur: ")[1].split("\n")[0])
            if "Taraf: boga" in user:
                rsi = flatten(extract_bundle_raw(user)["teknik"])["rsi14"]
                args = [Argument(claim="RSI güçlü momentum", source_analyst="teknik", evidence_field="rsi14", evidence_value=rsi)]
                if self.bull_hallucinates:
                    args.append(Argument(claim="Uydurma metrik", source_analyst="teknik", evidence_field="altin_oran", evidence_value=1.6))
                return DebateTurn(side="boga", round=rnd, thesis="Yükseliş trendi sürüyor", arguments=args, rebuttals=[])
            if rnd in self.bear_empty_rounds:
                args = [Argument(claim="Yanlış F/K", source_analyst="temel", evidence_field="pe_ratio", evidence_value=99.0)]
            else:
                args = [Argument(claim="Haber akışı zayıf", source_analyst="gundem", evidence_field="key_points", evidence_value=None)]
            return DebateTurn(side="ayi", round=rnd, thesis="Riskler fiyatlanmadı", arguments=args, rebuttals=["RSI aşırıya yakın"])
        if schema is RiskJudgement:
            return RiskJudgement(decision=self.decision, confidence=self.confidence, winning_side="boga",
                                 technical_fundamental_conflict=False, rationale="Boğa argümanları daha güçlü",
                                 key_risks=["Makro belirsizlik"])
        raise AssertionError(schema)


def extract_bundle_raw(user: str) -> dict:
    import json
    import re
    return json.loads(re.search(r"Ham veri \(anahtar adları için\):\n(.*?)\n\n", user, re.S).group(1))


@pytest.fixture
def provider(ohlcv):
    def _p(t):
        b = make_bundle(ohlcv, ticker=t)
        ind = b["market"]["indicators"]
        # Kontrollü seviye: hedef direnç uzakta -> R/Ö kapısından geçen bir senaryo
        ind["levels"]["resistance_60d"] = round(ind["close"] + 8 * ind["atr14"], 4)
        return b
    return _p


def test_end_to_end_al_with_retry_and_rejected_argument(provider):
    llm = FakeLLM(Script(liar_times={"teknik": 1}))
    progress = []
    st = run_analysis("TEST.IS", llm, data_provider=provider, progress=progress.append)

    assert st["attempts"] == {"teknik": 2, "temel": 1, "gundem": 1, "duygu": 1}
    assert all(c.passed for c in st["checks"].values())
    assert st["round"] == 2
    assert len(st["debate"]) == 4
    assert any("altin_oran" in r for v in st["debate"] for r in v.rejected)
    f = st["final"]
    assert f.decision == "AL"
    p = f.trade_plan
    assert p.stop_loss < p.entry_low <= p.entry_high < p.target
    assert p.risk_reward >= risk_manager.MIN_RISK_REWARD
    # model katmanlaması
    tiers = {(c["schema"], c["tier"]) for c in llm.calls}
    assert ("AnalystReport", "fast") in tiers
    assert ("DebateTurn", "reasoning") in tiers and ("RiskJudgement", "reasoning") in tiers
    assert not any(c["schema"] == "AnalystReport" and c["tier"] == "reasoning" for c in llm.calls)
    assert any("Boğa/Ayı" in m for m in progress) and any("Veriler çekiliyor" in m for m in progress)


def test_persistent_liar_capped_at_three_attempts_and_penalized(provider):
    llm = FakeLLM(Script(liar_times={"temel": 99}, confidence=0.75))
    st = run_analysis("TEST.IS", llm, data_provider=provider)
    assert st["attempts"]["temel"] == 3  # 1 + 2 yeniden deneme
    assert not st["checks"]["temel"].passed
    f = st["final"]
    assert f.confidence < f.raw_confidence
    assert f.decision == "BEKLE"  # 0.75 - 0.10 < 0.70
    assert f.trade_plan is None


def test_low_confidence_forces_bekle(provider):
    st = run_analysis("TEST.IS", FakeLLM(Script(confidence=0.65)), data_provider=provider)
    assert st["final"].decision == "BEKLE"
    assert any("eşik" in g for g in st["final"].gate_reasons)


def test_tech_fundamental_conflict_forces_bekle(provider):
    st = run_analysis("TEST.IS", FakeLLM(Script(conflict=True, confidence=0.95)), data_provider=provider)
    assert st["final"].decision == "BEKLE"
    assert any("çelişki" in g for g in st["final"].gate_reasons)


def test_missing_news_source_lowers_confidence(ohlcv):
    prov = lambda t: {**make_bundle(ohlcv, t, news_ok=False), "macro": {"status": "veri_yok", "macro": "veri_yok"}}
    st = run_analysis("TEST.IS", FakeLLM(Script(confidence=0.90)), data_provider=prov)
    f = st["final"]
    assert any("gundem: veri_yok" in g for g in f.gate_reasons)
    assert f.confidence == pytest.approx(0.80)


def test_extra_round_when_side_has_no_valid_argument_and_cap(provider):
    st = run_analysis("TEST.IS", FakeLLM(Script(bear_empty_rounds={1, 2, 3})), data_provider=provider)
    assert st["round"] == 3  # max_debate_rounds sınırı


def test_trade_plan_math():
    ind = {"close": 100.0, "atr14": 2.0,
           "levels": {"support_20d": 97.0, "resistance_20d": 103.0, "support_60d": 90.0, "resistance_60d": 110.0}}
    al = risk_manager.compute_trade_plan("AL", ind)
    assert (al.entry_low, al.entry_high, al.stop_loss, al.target) == (99.0, 100.0, 96.5, 110.0)
    assert al.risk_reward == round((110 - 99.5) / (99.5 - 96.5), 2)
    sat = risk_manager.compute_trade_plan("SAT", ind)
    assert (sat.entry_low, sat.entry_high, sat.stop_loss, sat.target) == (100.0, 101.0, 103.5, 90.0)
    assert sat.risk_reward == round((100.5 - 90) / (103.5 - 100.5), 2)
    assert risk_manager.compute_trade_plan("AL", {"close": 100.0, "atr14": "veri_yok"}) is None


def test_cli_end_to_end(monkeypatch, provider, tmp_path, capsys):
    import graph.workflow as wf
    import agents.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM(Script()))
    monkeypatch.setattr(wf, "default_data_provider", provider)
    monkeypatch.setattr(wf.run_analysis, "__defaults__", (provider, None))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import memory.db as dbm
    monkeypatch.setattr(dbm, "default_db_path", lambda: str(tmp_path / "t.db"))
    assert main_mod.main(["analiz", "TEST.IS"]) == 0
    out = capsys.readouterr().out
    assert "KARAR: AL" in out and "Stop-loss" in out and "Tahmin kaydedildi" in out


def test_low_risk_reward_forces_bekle(ohlcv):
    def prov(t):
        b = make_bundle(ohlcv, ticker=t)
        ind = b["market"]["indicators"]
        ind["levels"]["resistance_60d"] = round(ind["close"] + 0.5 * ind["atr14"], 4)  # hedef çok yakın
        ind["levels"]["support_20d"] = round(ind["close"] - 2.9 * ind["atr14"], 4)     # stop uzak
        return b
    st = run_analysis("TEST.IS", FakeLLM(Script(confidence=0.95)), data_provider=prov)
    assert st["final"].decision == "BEKLE"
    assert any("risk/ödül" in g for g in st["final"].gate_reasons)


def test_report_formatting(provider):
    from graph.report import format_report
    st = run_analysis("TEST.IS", FakeLLM(Script()), data_provider=provider)
    txt = format_report(st)
    assert "KARAR: AL" in txt and "Risk/Ödül" in txt and "yatırım tavsiyesi değildir" in txt


def test_llm_fully_down_returns_bekle_without_crash(provider):
    from agents.llm import LLMError

    def down(schema, system, user):
        raise LLMError("kota doldu")
    llm = FakeLLM(down)
    st = run_analysis("TEST.IS", llm, data_provider=provider)
    assert st["final"].decision == "BEKLE" and st["final"].confidence == 0.0
    assert any("LLM devre dışı" in g for g in st["final"].gate_reasons)
    assert not any(c["schema"] in ("DebateTurn", "RiskJudgement") for c in llm.calls)  # boşuna çağrı yok


def test_llm_fails_mid_debate_returns_bekle(provider):
    from agents.llm import LLMError
    base = Script()

    def flaky(schema, system, user):
        if schema is DebateTurn:
            raise LLMError("503")
        return base(schema, system, user)
    llm = FakeLLM(flaky)
    st = run_analysis("TEST.IS", llm, data_provider=provider)
    assert st["final"].decision == "BEKLE"
    assert sum(c["schema"] == "DebateTurn" for c in llm.calls) == 1
