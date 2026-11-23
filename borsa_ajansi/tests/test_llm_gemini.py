from types import SimpleNamespace

import pytest
from google.genai import errors

from agents.llm import LLMError, RoutedLLM, _GeminiStructured
from verification.schemas import RiskJudgement

GOOD = RiskJudgement(decision="BEKLE", confidence=0.5, winning_side="berabere",
                     technical_fundamental_conflict=False, rationale="r", key_risks=[]).model_dump_json()


@pytest.fixture(autouse=True)
def _reset_blocks(monkeypatch):
    import agents.llm as m
    monkeypatch.setattr(m, "_MODEL_BLOCKED_UNTIL", {})


def _err(code):
    return errors.APIError(code, {"error": {"code": code, "message": f"e{code}", "status": "X"}})


class FakeModels:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


def _run(script, models=("m1", "m2"), rounds=2):
    fm = FakeModels(script)
    r = _GeminiStructured(SimpleNamespace(models=fm), RiskJudgement, list(models), rounds=rounds, sleep=lambda s: None)
    return r, fm


def test_falls_back_on_503_404_and_bad_json():
    r, fm = _run([_err(503), _err(404), "{bozuk", GOOD], rounds=3)
    out = r.invoke("sys", "user")
    assert out.decision == "BEKLE"
    assert fm.calls == ["m1", "m2", "m1", "m1"]  # 404 veren m2 zincirden çıkarıldı


def test_non_retryable_error_raises():
    r, _ = _run([_err(400)])
    with pytest.raises(LLMError, match="400"):
        r.invoke("s", "u")


def test_gives_up_after_rounds():
    r, fm = _run([_err(503)] * 4)
    with pytest.raises(LLMError, match="tüm modeller"):
        r.invoke("s", "u")
    assert len(fm.calls) == 4


def test_routed_llm_picks_by_tier():
    fast, reasoning = object(), object()
    mk = lambda tag: SimpleNamespace(with_structured_output=lambda schema, tier: tag)
    r = RoutedLLM(mk("F"), mk("R"))
    assert r.with_structured_output(RiskJudgement, "fast") == "F"
    assert r.with_structured_output(RiskJudgement, "reasoning") == "R"


def _quota_err():
    return errors.APIError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}, {"retryDelay": "1000s"}]}})


def test_daily_quota_blocks_model_for_process():
    import agents.llm as m
    r, fm = _run([_quota_err(), GOOD], models=("q1", "q2"))
    assert r.invoke("s", "u").decision == "BEKLE"
    assert "q1" in m.blocked_models()
    r2, fm2 = _run([GOOD], models=("q1", "q2"))
    r2.invoke("s", "u")
    assert fm2.calls == ["q2"]  # kotası dolan model atlandı
    r3, fm3 = _run([], models=("q1",))
    with pytest.raises(LLMError, match="günlük kotası"):
        r3.invoke("s", "u")
    assert fm3.calls == []
