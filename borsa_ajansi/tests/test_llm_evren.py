import json
from types import SimpleNamespace

import pytest

from agents.llm import FallbackLLM, LLMError, _OpenAICompatStructured
from verification.schemas import RiskJudgement

GOOD = RiskJudgement(decision="BEKLE", confidence=0.5, winning_side="berabere",
                     technical_fundamental_conflict=False, rationale="r", key_risks=[]).model_dump_json()


def _resp(status, content=None, finish="stop", text=""):
    data = {"choices": [{"message": {"content": content}, "finish_reason": finish}]} if content is not None else {}
    return SimpleNamespace(status_code=status, json=lambda: data, text=text or json.dumps(data))


class FakePost:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, url, json, timeout, headers):
        self.calls.append((json["model"], "reasoning_effort" in json))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run(script, models=("a", "b"), effort=None):
    fp = FakePost(script)
    return _OpenAICompatStructured("https://x/v1", "k", RiskJudgement, list(models), 5, effort, post=fp,
                                   sleep=lambda s: None), fp


def test_success_and_strips_code_fence():
    r, fp = _run([_resp(200, "```json\n" + GOOD + "\n```")])
    assert r.invoke("s", "u").decision == "BEKLE"
    assert fp.calls == [("a", False)]


def test_falls_back_on_503_timeout_bad_json_and_truncation():
    r, fp = _run([_resp(503, text="busy"), TimeoutError("yavaş"), _resp(200, "{bozuk"), _resp(200, GOOD, "length"),
                  _resp(200, GOOD)], models=("a", "b", "c", "d", "e"))
    assert r.invoke("s", "u").decision == "BEKLE"
    assert [m for m, _ in fp.calls] == ["a", "b", "c", "d", "e"]


def test_auth_or_terms_error_raises_immediately():
    r, fp = _run([_resp(403, text='{"code":"terms_not_accepted"}')])
    with pytest.raises(LLMError, match="403"):
        r.invoke("s", "u")
    assert len(fp.calls) == 1


def test_unsupported_reasoning_effort_retried_without_it():
    r, fp = _run([_resp(400, text="unsupported"), _resp(200, GOOD)], models=("a",), effort="low")
    assert r.invoke("s", "u").decision == "BEKLE"
    assert fp.calls == [("a", True), ("a", False)]


def test_all_fail_raises():
    r, _ = _run([_resp(500), _resp(500)])
    with pytest.raises(LLMError, match="tüm modeller"):
        r.invoke("s", "u")


@pytest.fixture(autouse=True)
def _reset_cooldown(monkeypatch):
    import agents.llm as m
    monkeypatch.setattr(m, "_PROVIDER_DOWN_UNTIL", {})


def test_failed_provider_is_skipped_during_cooldown():
    import agents.llm as m
    calls = []

    def mk(name, fn):
        def inv(s, u):
            calls.append(name)
            return fn(s, u)
        return SimpleNamespace(invoke=inv)

    def boom(s, u):
        raise LLMError("evren zaman aşımı")
    t = [1000.0]
    fb = m._FallbackStructured([("evren", mk("evren", boom)), ("gemini", mk("gemini", lambda s, u: "ok"))],
                               clock=lambda: t[0])
    assert fb.invoke("s", "u") == "ok" and calls == ["evren", "gemini"]
    assert fb.invoke("s", "u") == "ok" and calls[-1] == "gemini" and calls.count("evren") == 1  # atlandı
    t[0] += 601
    fb.invoke("s", "u")
    assert calls.count("evren") == 2  # süre dolunca tekrar denendi


def test_fallback_llm_uses_next_provider():
    def mk(fn):
        return SimpleNamespace(with_structured_output=lambda schema, tier: SimpleNamespace(invoke=fn))

    def boom(s, u):
        raise LLMError("evren kapalı")
    ok = mk(lambda s, u: "gemini-sonucu")
    assert FallbackLLM([mk(boom), ok]).with_structured_output(RiskJudgement, "fast").invoke("s", "u") == "gemini-sonucu"
    with pytest.raises(LLMError, match="evren kapalı"):
        FallbackLLM([mk(boom)]).with_structured_output(RiskJudgement, "fast").invoke("s", "u")


def test_429_waits_and_retries_same_model():
    r, fp = _run([_resp(429, text="busy"), _resp(200, GOOD)])
    assert r.invoke("s", "u").decision == "BEKLE"
    assert [m for m, _ in fp.calls] == ["a", "a"]


def test_provider_routing(monkeypatch):
    import agents.llm as m
    from config.settings import settings
    old_e, old_g = settings.evren_api_key, settings.gemini_api_key
    object.__setattr__(settings, "evren_api_key", "k")
    object.__setattr__(settings, "gemini_api_key", "g")
    try:
        g = m._provider("gemini")
        e = m._provider("evren")
        assert [type(p).__name__ for p in g.providers] == ["GeminiLLM", "EvrenLLM"]
        assert [type(p).__name__ for p in e.providers] == ["EvrenLLM", "GeminiLLM"]
    finally:
        object.__setattr__(settings, "evren_api_key", old_e)
        object.__setattr__(settings, "gemini_api_key", old_g)
