"""Merkezi ayarlar: tüm gizli bilgiler yalnızca .env'den okunur (koda gömülmez)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

VERI_YOK = "veri_yok"


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _int_list(name: str) -> list[int]:
    out: list[int] = []
    for x in _csv(name):
        try:
            out.append(int(x))
        except ValueError:
            pass
    return out


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    # Maliyet katmanlaması: hızlı model analistler/özet, güçlü model tartışma + risk
    fast_model: str = field(default_factory=lambda: os.getenv("FAST_MODEL", "claude-haiku-5-5"))
    reasoning_model: str = field(default_factory=lambda: os.getenv("REASONING_MODEL", "claude-opus-5-5"))
    fast_effort: str = field(default_factory=lambda: os.getenv("FAST_EFFORT", "low"))
    reasoning_effort: str = field(default_factory=lambda: os.getenv("REASONING_EFFORT", "medium"))

    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    # Yalnızca ana anahtar hesap/kredi hatası (401/402/403) verdiğinde devreye girer
    gemini_timeout_s: float = field(default_factory=lambda: float(os.getenv("GEMINI_TIMEOUT_S", "120")))
    gemini_api_key_backup: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY_BACKUP") or None)
    gemini_fast_models: list[str] = field(default_factory=lambda: _csv(
        "GEMINI_FAST_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemini-2.5-flash-lite,gemini-3.6-flash"))
    gemini_reasoning_models: list[str] = field(default_factory=lambda: _csv(
        "GEMINI_REASONING_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-pro,gemini-2.5-flash"))
    # EVREN (yerli, OpenAI uyumlu LLM ağ geçidi)
    evren_api_key: str | None = field(default_factory=lambda: os.getenv("EVREN_API_KEY") or None)
    evren_base_url: str = field(default_factory=lambda: os.getenv("EVREN_BASE_URL", "https://evren-llmapi.ssyz.org.tr/v1"))
    evren_fast_models: list[str] = field(default_factory=lambda: _csv(
        "EVREN_FAST_MODELS", "deepseek-v4.1-flash,qwen3.8-flash-next"))
    evren_reasoning_models: list[str] = field(default_factory=lambda: _csv(
        "EVREN_REASONING_MODELS", "glm-5.3,deepseek-v4.1-flash"))
    evren_timeout_s: float = field(default_factory=lambda: float(os.getenv("EVREN_TIMEOUT_S", "150")))
    # Aynı anda EVREN'e giden en fazla istek (fazlası 429 / kuyruk zaman aşımı üretiyor)
    evren_max_concurrency: int = field(default_factory=lambda: int(os.getenv("EVREN_MAX_CONCURRENCY", "2")))
    # auto: ANTHROPIC_API_KEY varsa Claude; yoksa EVREN (Gemini yedekli); yoksa Gemini
    fast_provider: str = field(default_factory=lambda: os.getenv("FAST_PROVIDER", "auto"))
    reasoning_provider: str = field(default_factory=lambda: os.getenv("REASONING_PROVIDER", "auto"))

    telegram_bot_token: str | None = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN"))
    allowed_telegram_user_ids: list[int] = field(default_factory=lambda: _int_list("ALLOWED_TELEGRAM_USER_ID"))
    watchlist: list[str] = field(default_factory=lambda: _csv("WATCHLIST", "THYAO.IS,ASELS.IS,GARAN.IS"))
    morning_report_enabled: bool = field(default_factory=lambda: os.getenv("MORNING_REPORT_ENABLED", "true").lower() == "true")
    report_hour: int = field(default_factory=lambda: int(os.getenv("REPORT_HOUR", "9")))
    report_minute: int = field(default_factory=lambda: int(os.getenv("REPORT_MINUTE", "30")))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Europe/Istanbul"))

    # ---- Sanal portföy simülasyonu (gerçek para yok) ----
    universe: list[str] = field(default_factory=lambda: _csv("UNIVERSE", ""))  # boş -> BIST100
    # Taramada elenecek sığ hisse eşiği: 20 günlük ort. işlem hacmi (milyon TL); 0 = filtre yok
    screen_min_turnover_mtl: float = field(default_factory=lambda: float(os.getenv("SCREEN_MIN_TURNOVER_MTL", "50")))
    sim_initial_cash: float = field(default_factory=lambda: float(os.getenv("SIM_INITIAL_CASH", "10000")))
    sim_days: int = field(default_factory=lambda: int(os.getenv("SIM_DAYS", "14")))  # takvim günü
    sim_candidates: int = field(default_factory=lambda: int(os.getenv("SIM_CANDIDATES", "3")))  # LLM'e giden aday/gün
    sim_max_positions: int = field(default_factory=lambda: int(os.getenv("SIM_MAX_POSITIONS", "4")))
    sim_risk_per_trade_pct: float = field(default_factory=lambda: float(os.getenv("SIM_RISK_PER_TRADE_PCT", "2")))
    sim_max_position_pct: float = field(default_factory=lambda: float(os.getenv("SIM_MAX_POSITION_PCT", "30")))
    sim_commission_pct: float = field(default_factory=lambda: float(os.getenv("SIM_COMMISSION_PCT", "0.2")))
    sim_slippage_pct: float = field(default_factory=lambda: float(os.getenv("SIM_SLIPPAGE_PCT", "0.1")))
    sim_trade_hour: int = field(default_factory=lambda: int(os.getenv("SIM_TRADE_HOUR", "10")))
    sim_trade_minute: int = field(default_factory=lambda: int(os.getenv("SIM_TRADE_MINUTE", "30")))
    sim_eod_hour: int = field(default_factory=lambda: int(os.getenv("SIM_EOD_HOUR", "18")))
    sim_eod_minute: int = field(default_factory=lambda: int(os.getenv("SIM_EOD_MINUTE", "30")))

    # KAP bildirim metinleri + faaliyet raporu/sunum okuma (her hisse için +2 LLM çağrısı, önbellekli)
    documents_enabled: bool = field(default_factory=lambda: os.getenv("DOCUMENTS_ENABLED", "true").lower() == "true")

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(BASE_DIR / "borsa_ajansi.db")))

    # Eşikler ve döngü limitleri
    confidence_threshold: float = 0.70
    max_fact_check_retries: int = 2
    min_debate_rounds: int = field(default_factory=lambda: int(os.getenv("MIN_DEBATE_ROUNDS", "2")))
    max_debate_rounds: int = field(default_factory=lambda: int(os.getenv("MAX_DEBATE_ROUNDS", "3")))
    price_tolerance_pct: float = 1.0      # fiyat iddiası için % tolerans
    indicator_tolerance: float = 2.0      # RSI vb. için mutlak tolerans
    ratio_tolerance_pct: float = 5.0      # F/K vb. için % tolerans
    veri_yok_confidence_penalty: float = 0.10  # her eksik kaynak için güven düşüşü


settings = Settings()
