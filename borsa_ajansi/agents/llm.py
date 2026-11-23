"""Claude erişim katmanı: her çağrı Pydantic şemasına bağlı yapılandırılmış çıktı döndürür.

`with_structured_output(schema, tier)` arayüzü, ajanların düz metin üretmesini imkânsız kılar.
tier="fast" -> FAST_MODEL (analistler), tier="reasoning" -> REASONING_MODEL (boğa/ayı, risk).
"""
from __future__ import annotations

import logging
from typing import Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

from config.settings import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
Tier = Literal["fast", "reasoning"]


class LLMError(RuntimeError):
    pass


class StructuredRunnable(Protocol[T]):
    def invoke(self, system: str, user: str) -> T: ...


class LLM(Protocol):
    def with_structured_output(self, schema: type[T], tier: Tier) -> StructuredRunnable[T]: ...


class _ClaudeStructured(Generic[T]):
    def __init__(self, client, schema: type[T], model: str, effort: str):
        self.client, self.schema, self.model, self.effort = client, schema, model, effort

    def invoke(self, system: str, user: str) -> T:
        import anthropic

        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=self.schema,
                output_config={"effort": self.effort},
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API hatası ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Claude API bağlantı hatası: {exc}") from exc

        if resp.stop_reason == "refusal":
            raise LLMError("Model isteği reddetti (refusal)")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Çıktı max_tokens sınırında kesildi")
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError("Yapılandırılmış çıktı ayrıştırılamadı")
        return parsed


class ClaudeLLM:
    def __init__(self, client=None):
        import anthropic

        if client is None:
            if not settings.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY .env içinde tanımlı değil")
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
        self.client = client

    def with_structured_output(self, schema: type[T], tier: Tier) -> _ClaudeStructured[T]:
        if tier == "fast":
            return _ClaudeStructured(self.client, schema, settings.fast_model, settings.fast_effort)
        return _ClaudeStructured(self.client, schema, settings.reasoning_model, settings.reasoning_effort)


# ---------------------------------------------------------------------------
# Google Gemini sağlayıcısı
# ---------------------------------------------------------------------------
_RETRYABLE = {429, 500, 502, 503, 504}

# Süreç genelinde: günlük kotası biten / kaldırılmış modeller -> bu zamana (epoch sn) kadar atlanır
_MODEL_BLOCKED_UNTIL: dict[str, float] = {}
_BLOCK_LOCK = __import__("threading").Lock()


def _daily_quota_delay(exc) -> float | None:
    """429 günlük kota ihlaliyse beklenecek saniyeyi döndürür (dakikalık sınırlarda None)."""
    details = (getattr(exc, "details", None) or {}).get("error", {}).get("details") or []
    per_day, delay = False, None
    for d in details:
        for v in d.get("violations", []) or []:
            if "PerDay" in (v.get("quotaId") or ""):
                per_day = True
        if d.get("retryDelay"):
            try:
                delay = float(str(d["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    return (delay or 3600.0) if per_day else None


def blocked_models() -> dict[str, float]:
    import time

    now = time.time()
    with _BLOCK_LOCK:
        return {m: t for m, t in _MODEL_BLOCKED_UNTIL.items() if t > now}


class _GeminiStructured(Generic[T]):
    """Model zinciri: model yoğunsa (503) sıradakine geçer; günlük kotası biten modeli süreç boyunca atlar;
    tüm modeller kullanılamazsa hemen LLMError (boşuna bekleme yok)."""

    def __init__(self, client, schema: type[T], models: list[str], rounds: int = 2, base_sleep: float = 3.0,
                 sleep=None):
        import time

        self.client, self.schema, self.models = client, schema, models
        self.rounds, self.base_sleep = rounds, base_sleep
        self.sleep = sleep or time.sleep

    def _block(self, model: str, seconds: float) -> None:
        import time

        with _BLOCK_LOCK:
            _MODEL_BLOCKED_UNTIL[model] = time.time() + seconds

    def invoke(self, system: str, user: str) -> T:
        from google.genai import errors, types
        from pydantic import ValidationError

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=self.schema.model_json_schema(),
            temperature=0.2,
        )
        last: Exception | None = None
        for rnd in range(self.rounds):
            usable = [m for m in self.models if m not in blocked_models()]
            if not usable:
                raise LLMError("Gemini: zincirdeki tüm modellerin günlük kotası dolu / model kullanılamıyor")
            for model in usable:
                try:
                    resp = self.client.models.generate_content(model=model, contents=user, config=cfg)
                    text = resp.text or ""
                    return self.schema.model_validate_json(text)
                except errors.APIError as exc:
                    last = exc
                    if exc.code == 404:
                        self._block(model, 86400)
                        log.warning("Gemini %s kullanılamıyor (404), zincirden çıkarıldı", model)
                        continue
                    if exc.code == 429 and (delay := _daily_quota_delay(exc)) is not None:
                        self._block(model, delay)
                        log.warning("Gemini %s günlük kotası doldu, %.1f saat atlanacak", model, delay / 3600)
                        continue
                    if exc.code in _RETRYABLE:
                        log.warning("Gemini %s geçici hata %s, sıradaki model deneniyor", model, exc.code)
                        continue
                    raise LLMError(f"Gemini API hatası ({exc.code}): {exc.message}") from exc
                except ValidationError as exc:
                    last = exc
                    log.warning("Gemini %s şemaya uymayan çıktı verdi, sıradaki model", model)
                    continue
            if rnd < self.rounds - 1:
                self.sleep(self.base_sleep * (2 ** rnd))
        raise LLMError(f"Gemini: tüm modeller başarısız ({type(last).__name__}: {str(last)[:200]})")


class GeminiLLM:
    def __init__(self, client=None):
        if client is None:
            from google import genai

            if not settings.gemini_api_key:
                raise LLMError("GEMINI_API_KEY .env içinde tanımlı değil")
            client = genai.Client(api_key=settings.gemini_api_key, http_options={"api_version": "v1beta"})
        self.client = client

    def with_structured_output(self, schema: type[T], tier: Tier) -> _GeminiStructured[T]:
        models = settings.gemini_fast_models if tier == "fast" else settings.gemini_reasoning_models
        return _GeminiStructured(self.client, schema, models)


class RoutedLLM:
    """Katman bazında sağlayıcı seçimi (ör. analistler Gemini, tartışma/risk Claude)."""

    def __init__(self, fast: LLM, reasoning: LLM):
        self.fast, self.reasoning = fast, reasoning

    def with_structured_output(self, schema: type[T], tier: Tier):
        return (self.fast if tier == "fast" else self.reasoning).with_structured_output(schema, tier)


def _provider(name: str) -> LLM:
    if name == "auto":
        name = "anthropic" if settings.anthropic_api_key else "gemini"
    if name == "anthropic":
        return ClaudeLLM()
    if name == "gemini":
        return GeminiLLM()
    raise LLMError(f"Bilinmeyen LLM sağlayıcısı: {name}")


def get_llm() -> LLM:
    """FAST_PROVIDER / REASONING_PROVIDER: auto | anthropic | gemini."""
    fast, reasoning = settings.fast_provider, settings.reasoning_provider
    if fast == reasoning:
        return _provider(fast)
    return RoutedLLM(_provider(fast), _provider(reasoning))
