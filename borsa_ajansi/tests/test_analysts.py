import os

import pytest

from agents import analysts
from agents.llm import LLMError
from tests.fakes import FakeLLM, extract_analyst, extract_raw, make_bundle
from verification import fact_checker
from verification.schemas import AnalystReport, NumericClaim

FIELDS = {"teknik": ["close", "rsi14", "levels.support_20d"], "temel": ["pe_ratio", "net_debt_to_ebitda"],
          "gundem": ["news_count"], "duygu": ["rsi14", "dist_from_ma50_pct"]}


def honest_responder(schema, system, user):
    """Sayıları ham veriden doğru kopyalayan 'dürüst' analist."""
    from tests.fakes import extract_analyst
    from verification.fact_checker import flatten

    name = extract_analyst(user)
    raw = flatten(extract_raw(user))
    claims = [NumericClaim(field=f, value=raw[f]) for f in FIELDS[name] if isinstance(raw.get(f), (int, float))]
    return AnalystReport(analyst=name, ticker="TEST.IS", data_date=raw.get("date", "veri_yok"), signal="olumlu",
                         score=0.4, confidence=0.7, key_points=["x"], claims=claims, data_gaps=[], summary="ok")


def test_raw_data_routing(ohlcv):
    b = make_bundle(ohlcv)
    assert "rsi14" in analysts.raw_data_for("teknik", b)
    assert analysts.raw_data_for("temel", b)["pe_ratio"] == 6.2
    assert analysts.raw_data_for("gundem", b)["news_count"] == 3
    assert "rsi_zone" in analysts.raw_data_for("duygu", b)
    for name in analysts.ANALYSTS:
        assert analysts.raw_data_for(name, b)["date"] == b["market"]["indicators"]["date"]


def test_parallel_analysts_use_fast_model_and_pass_fact_check(ohlcv):
    b = make_bundle(ohlcv)
    llm = FakeLLM(honest_responder)
    reports = analysts.run_analysts_parallel(llm, list(analysts.ANALYSTS), "TEST.IS", b)
    assert set(reports) == set(analysts.ANALYSTS)
    assert all(c["tier"] == "fast" for c in llm.calls)
    for name, rep in reports.items():
        res = fact_checker.check_report(rep, analysts.raw_data_for(name, b), expected_ticker="TEST.IS")
        assert res.passed, res.errors
        assert rep.claims, f"{name} en az bir iddia içermeli"


def test_fact_checker_catches_wrong_number(ohlcv):
    b = make_bundle(ohlcv)
    raw = analysts.raw_data_for("teknik", b)
    rep = AnalystReport(analyst="teknik", ticker="TEST.IS", data_date=raw["date"], signal="olumlu", score=0.5,
                        confidence=0.8, key_points=[], data_gaps=[], summary="",
                        claims=[NumericClaim(field="rsi14", value=raw["rsi14"] + 10),
                                NumericClaim(field="close", value=raw["close"] * 1.005)])
    res = fact_checker.check_report(rep, raw)
    assert not res.passed
    assert any("rsi14" in e for e in res.errors)
    assert all("close" not in e for e in res.errors)  # %0.5 sapma tolerans içinde


def test_fact_checker_catches_hallucinated_field_and_veri_yok(ohlcv):
    b = make_bundle(ohlcv.tail(30))  # ma200 = veri_yok
    raw = analysts.raw_data_for("teknik", b)
    rep = AnalystReport(analyst="teknik", ticker="TEST.IS", data_date=raw["date"], signal="olumlu", score=0.5,
                        confidence=0.8, key_points=[], data_gaps=[], summary="",
                        claims=[NumericClaim(field="ma200", value=100.0), NumericClaim(field="fibonacci_618", value=1.0)])
    res = fact_checker.check_report(rep, raw)
    assert not res.passed
    assert any("halüsinasyon" in e for e in res.errors)
    assert any("fibonacci_618" in e for e in res.errors)


def test_fact_checker_catches_date_and_ticker(ohlcv):
    b = make_bundle(ohlcv)
    raw = analysts.raw_data_for("teknik", b)
    rep = AnalystReport(analyst="teknik", ticker="XXX.IS", data_date="1999-01-01", signal="notr", score=0,
                        confidence=0.5, key_points=[], claims=[], data_gaps=[], summary="")
    res = fact_checker.check_report(rep, raw, expected_ticker="TEST.IS")
    assert len(res.errors) == 2


def test_feedback_is_sent_on_retry(ohlcv):
    b = make_bundle(ohlcv)
    llm = FakeLLM(honest_responder)
    analysts.run_analysts_parallel(llm, ["teknik"], "TEST.IS", b, feedback={"teknik": ["'rsi14': rapor=99"]})
    assert "doğrulamadan GEÇEMEDİ" in llm.calls[0]["user"]
    assert "rsi14" in llm.calls[0]["user"]


def test_llm_failure_yields_veri_yok_report(ohlcv):
    def boom(schema, system, user):
        raise LLMError("api down")
    reports = analysts.run_analysts_parallel(FakeLLM(boom), ["temel"], "TEST.IS", make_bundle(ohlcv))
    assert reports["temel"].confidence == 0.0
    assert reports["temel"].data_date == "veri_yok"


@pytest.mark.llm
@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY yok")
def test_real_claude_technical_analyst(ohlcv):
    from agents.llm import ClaudeLLM
    b = make_bundle(ohlcv)
    raw = analysts.raw_data_for("teknik", b)
    rep = analysts.run_analyst(ClaudeLLM(), "teknik", "TEST.IS", raw)
    res = fact_checker.check_report(rep, raw, expected_ticker="TEST.IS")
    assert res.passed, res.errors
