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


_ACCOUNT_ERRORS = {401, 402, 403}  # geçersiz anahtar / kredi bitti / yetki yok -> o anahtarın tümü kullanılamaz


def _is_transport_error(exc: Exception) -> bool:
    """httpx / httpx2 (google-genai) zaman aşımı ve ağ hataları; sınıf adına bakılır, iki kütüphaneyi de kapsar."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(c.__name__ in ("TimeoutException", "TransportError") for c in type(exc).__mro__)


def _bkey(label: str, model: str) -> str:
    return f"{label}/{model}" if label else model


class _GeminiStructured(Generic[T]):
    """Anahtar + model zinciri.

    - 503/5xx: sıradaki modele geçer, tur sonunda artan bekleme ile tekrar dener.
    - 404: model zincirden çıkarılır. 429 (günlük kota): o anahtar için model süreç boyunca atlanır.
    - 401/402/403 (hesap/kredi): o anahtarın tamamı bloklanır, varsa yedek anahtara geçilir.
    - Kullanılabilir hiçbir şey kalmazsa hemen LLMError (boşuna bekleme yok).
    """

    def __init__(self, client, schema: type[T], models: list[str], rounds: int = 3, base_sleep: float = 4.0,
                 sleep=None):
        import time

        # Geriye dönük uyum: tek istemci ya da [(etiket, istemci), ...]
        self.clients: list[tuple[str, object]] = client if isinstance(client, list) else [("", client)]
        self.schema, self.models = schema, list(dict.fromkeys(models))
        self.rounds, self.base_sleep = rounds, base_sleep
        self.sleep = sleep or time.sleep

    @staticmethod
    def _block(key: str, seconds: float) -> None:
        import time

        with _BLOCK_LOCK:
            _MODEL_BLOCKED_UNTIL[key] = time.time() + seconds

    def _usable(self) -> list[tuple[str, object, str]]:
        blocked = blocked_models()
        out = []
        for label, client in self.clients:
            if _bkey(label, "*") in blocked:
                continue
            out += [(label, client, m) for m in self.models if _bkey(label, m) not in blocked]
        return out

    def invoke(self, system: str, user: str) -> T:
        from google.genai import errors, types
        from pydantic import ValidationError

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=self.schema.model_json_schema(),
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        last: Exception | None = None
        account_msg: str | None = None
        for rnd in range(self.rounds):
            usable = self._usable()
            if not usable:
                if account_msg:
                    raise LLMError(f"Gemini hesabı kullanılamıyor: {account_msg}")
                raise LLMError("Gemini: zincirdeki tüm modellerin günlük kotası dolu / model kullanılamıyor")
            for label, client, model in usable:
                blocked = blocked_models()
                if _bkey(label, "*") in blocked or _bkey(label, model) in blocked:
                    continue  # bu tur içinde bloklandı
                try:
                    resp = client.models.generate_content(model=model, contents=user, config=cfg)
                    return self.schema.model_validate_json(resp.text or "")
                except errors.APIError as exc:
                    last = exc
                    tag = f"{label + ' ' if label else ''}{model}"
                    if exc.code in _ACCOUNT_ERRORS:
                        account_msg = f"({exc.code}) {str(exc.message)[:160]}"
                        self._block(_bkey(label, "*"), 3600 if exc.code == 402 else 86400)
                        log.error("Gemini anahtarı %s kullanılamıyor %s", label or "(ana)", account_msg)
                        continue
                    if exc.code == 404:
                        self._block(_bkey(label, model), 86400)
                        log.warning("Gemini %s kullanılamıyor (404), zincirden çıkarıldı", tag)
                        continue
                    if exc.code == 429 and (delay := _daily_quota_delay(exc)) is not None:
                        self._block(_bkey(label, model), delay)
                        log.warning("Gemini %s günlük kotası doldu, %.1f saat atlanacak", tag, delay / 3600)
                        continue
                    if exc.code in _RETRYABLE:
                        log.warning("Gemini %s geçici hata %s, sıradaki model deneniyor", tag, exc.code)
                        continue
                    raise LLMError(f"Gemini API hatası ({exc.code}): {exc.message}") from exc
                except ValidationError as exc:
                    last = exc
                    log.warning("Gemini %s şemaya uymayan çıktı verdi, sıradaki model", model)
                    continue
                except Exception as exc:  # zaman aşımı / bağlantı kopması: askıda kalmak yerine sıradaki model
                    if not _is_transport_error(exc):
                        raise
                    last = exc
                    log.warning("Gemini %s yanıt vermedi (%s), sıradaki model", model, type(exc).__name__)
                    continue
            if rnd < self.rounds - 1 and self._usable():
                self.sleep(self.base_sleep * (2 ** rnd))
        if account_msg and not self._usable():
            raise LLMError(f"Gemini hesabı kullanılamıyor: {account_msg}")
        raise LLMError(f"Gemini: tüm modeller başarısız ({type(last).__name__}: {str(last)[:200]})")


class GeminiLLM:
    def __init__(self, client=None):
        if client is None:
            from google import genai

            if not settings.gemini_api_key:
                raise LLMError("GEMINI_API_KEY .env içinde tanımlı değil")
            # Tek istek en fazla GEMINI_TIMEOUT_S sn; askıda kalan istek tüm döngüyü kilitlemesin
            opts = {"api_version": "v1beta", "timeout": int(settings.gemini_timeout_s * 1000)}
            client = [("ana", genai.Client(api_key=settings.gemini_api_key, http_options=opts))]
            if settings.gemini_api_key_backup:
                client.append(("yedek", genai.Client(api_key=settings.gemini_api_key_backup, http_options=opts)))
        self.client = client

    def with_structured_output(self, schema: type[T], tier: Tier) -> _GeminiStructured[T]:
        if tier == "fast":
            models = settings.gemini_fast_models
        else:
            # Güçlü modellerin hepsi yoğun/kotasızsa son çare olarak hızlı modellere düş
            models = settings.gemini_reasoning_models + settings.gemini_fast_models
        return _GeminiStructured(self.client, schema, models)


# ---------------------------------------------------------------------------
# EVREN (OpenAI uyumlu /chat/completions + json_schema yapılandırılmış çıktı)
# ---------------------------------------------------------------------------
_EVREN_SEM = None
_EVREN_SEM_LOCK = __import__("threading").Lock()


def _evren_semaphore():
    import threading

    global _EVREN_SEM
    with _EVREN_SEM_LOCK:
        if _EVREN_SEM is None:
            _EVREN_SEM = threading.BoundedSemaphore(max(1, settings.evren_max_concurrency))
        return _EVREN_SEM


class _OpenAICompatStructured(Generic[T]):
    """Model zinciri: 429/5xx/zaman aşımı -> sıradaki model; 401/403 (anahtar/şart onayı) -> hemen LLMError."""

    def __init__(self, base_url: str, api_key: str, schema: type[T], models: list[str], timeout: float,
                 reasoning_effort: str | None = None, post=None, label: str = "EVREN", sleep=None,
                 rate_retries: int = 2):
        import time

        import requests

        self.base_url, self.api_key, self.schema = base_url.rstrip("/"), api_key, schema
        self.models, self.timeout, self.effort, self.label = list(dict.fromkeys(models)), timeout, reasoning_effort, label
        self.post = post or requests.post
        self.sleep = sleep or time.sleep
        self.rate_retries = rate_retries

    def _send(self, body: dict):
        with _evren_semaphore():
            return self.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                             headers={"Authorization": f"Bearer {self.api_key}"})

    @staticmethod
    def _extract_json(text: str) -> str:
        t = (text or "").strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else t
            t = t.rsplit("```", 1)[0]
        return t.strip()

    def invoke(self, system: str, user: str) -> T:
        from pydantic import ValidationError

        last = "bilinmiyor"
        for model in self.models:
            body = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": self.schema.__name__, "schema": self.schema.model_json_schema(), "strict": True}},
                "temperature": 0.2,
                "max_tokens": 16000,
            }
            if self.effort:
                body["reasoning_effort"] = self.effort
            try:
                r = self._send(body)
                if r.status_code == 400 and "reasoning_effort" in body:  # model bu parametreyi desteklemiyor
                    body.pop("reasoning_effort")
                    r = self._send(body)
                for attempt in range(self.rate_retries):  # 429: eşzamanlılık sınırı -> bekle, aynı modeli tekrar dene
                    if r.status_code != 429:
                        break
                    wait = 5.0 * (attempt + 1)
                    log.warning("%s %s 429 (yoğun), %.0f sn sonra tekrar", self.label, model, wait)
                    self.sleep(wait)
                    r = self._send(body)
            except Exception as exc:  # zaman aşımı / bağlantı
                last = f"{model}: {type(exc).__name__}"
                log.warning("%s %s yanıt vermedi (%s), sıradaki model", self.label, model, type(exc).__name__)
                continue
            if r.status_code in (401, 403):
                raise LLMError(f"{self.label} erişim reddedildi ({r.status_code}): {r.text[:200]}")
            if r.status_code != 200:
                last = f"{model}: HTTP {r.status_code} {r.text[:150]}"
                log.warning("%s %s hata %s, sıradaki model", self.label, model, r.status_code)
                continue
            try:
                data = r.json()
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("çıktı uzunluk sınırında kesildi")
                return self.schema.model_validate_json(self._extract_json(choice["message"].get("content") or ""))
            except (ValidationError, ValueError, KeyError, IndexError, TypeError) as exc:
                last = f"{model}: {type(exc).__name__}: {str(exc)[:150]}"
                log.warning("%s %s şemaya uymayan çıktı (%s), sıradaki model", self.label, model, type(exc).__name__)
                continue
        raise LLMError(f"{self.label}: tüm modeller başarısız ({last})")


class EvrenLLM:
    def __init__(self, post=None, sleep=None):
        if not settings.evren_api_key:
            raise LLMError("EVREN_API_KEY .env içinde tanımlı değil")
        self.post, self.sleep = post, sleep

    def with_structured_output(self, schema: type[T], tier: Tier) -> _OpenAICompatStructured[T]:
        fast = tier == "fast"
        models = settings.evren_fast_models if fast else settings.evren_reasoning_models
        return _OpenAICompatStructured(settings.evren_base_url, settings.evren_api_key, schema, models,
                                       settings.evren_timeout_s, reasoning_effort="low" if fast else None,
                                       post=self.post, sleep=self.sleep)


_PROVIDER_COOLDOWN_S = 600
_PROVIDER_DOWN_UNTIL: dict[str, float] = {}


class _FallbackStructured:
    """Sağlayıcıları sırayla dener. Tamamen başarısız olan sağlayıcı 10 dk atlanır (her çağrıda yeniden
    dakikalarca zaman aşımı beklenmesin); hepsi bekleme süresindeyse yine de sırayla denenir."""

    def __init__(self, runnables: list[tuple[str, object]], clock=None):
        import time

        self.runnables, self.clock = runnables, clock or time.time

    def invoke(self, system: str, user: str):
        now = self.clock()
        fresh = [(n, r) for n, r in self.runnables if _PROVIDER_DOWN_UNTIL.get(n, 0) <= now]
        order = fresh or self.runnables
        errors_: list[str] = []
        for name, rn in order:
            try:
                out = rn.invoke(system, user)
                _PROVIDER_DOWN_UNTIL.pop(name, None)
                return out
            except LLMError as exc:
                errors_.append(str(exc)[:160])
                _PROVIDER_DOWN_UNTIL[name] = self.clock() + _PROVIDER_COOLDOWN_S
                log.warning("%s başarısız, %d dk atlanacak; yedeğe geçiliyor: %s", name,
                            _PROVIDER_COOLDOWN_S // 60, str(exc)[:160])
        raise LLMError(" | ".join(errors_) or "sağlayıcı yok")


class FallbackLLM:
    """Sağlayıcı zinciri: birincisi tamamen başarısız olursa sıradakine geçer (ör. EVREN -> Gemini)."""

    def __init__(self, providers: list):
        self.providers = providers

    def with_structured_output(self, schema: type[T], tier: Tier):
        return _FallbackStructured([(f"{type(p).__name__}:{tier}", p.with_structured_output(schema, tier))
                                    for p in self.providers])


class RoutedLLM:
    """Katman bazında sağlayıcı seçimi (ör. analistler Gemini, tartışma/risk Claude)."""

    def __init__(self, fast: LLM, reasoning: LLM):
        self.fast, self.reasoning = fast, reasoning

    def with_structured_output(self, schema: type[T], tier: Tier):
        return (self.fast if tier == "fast" else self.reasoning).with_structured_output(schema, tier)


def _provider(name: str) -> LLM:
    if name == "auto":
        name = "anthropic" if settings.anthropic_api_key else "evren" if settings.evren_api_key else "gemini"
    if name == "anthropic":
        return ClaudeLLM()
    if name == "gemini":
        # Gemini birincil, EVREN anahtarı varsa yedek
        return FallbackLLM([GeminiLLM(), EvrenLLM()]) if settings.evren_api_key else GeminiLLM()
    if name == "evren":
        # EVREN birincil, Gemini anahtarı varsa yedek
        return FallbackLLM([EvrenLLM(), GeminiLLM()]) if settings.gemini_api_key else EvrenLLM()
    raise LLMError(f"Bilinmeyen LLM sağlayıcısı: {name}")


def get_llm() -> LLM:
    """FAST_PROVIDER / REASONING_PROVIDER: auto | anthropic | evren | gemini."""
    fast, reasoning = settings.fast_provider, settings.reasoning_provider
    if fast == reasoning:
        return _provider(fast)
    return RoutedLLM(_provider(fast), _provider(reasoning))
