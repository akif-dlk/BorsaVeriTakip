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
