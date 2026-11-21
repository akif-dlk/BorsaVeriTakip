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

    telegram_bot_token: str | None = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN"))
    allowed_telegram_user_ids: list[int] = field(default_factory=lambda: _int_list("ALLOWED_TELEGRAM_USER_ID"))
    watchlist: list[str] = field(default_factory=lambda: _csv("WATCHLIST", "THYAO.IS,ASELS.IS,GARAN.IS"))
    report_hour: int = field(default_factory=lambda: int(os.getenv("REPORT_HOUR", "9")))
    report_minute: int = field(default_factory=lambda: int(os.getenv("REPORT_MINUTE", "30")))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Europe/Istanbul"))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(BASE_DIR / "borsa_ajansi.db")))

    # Eşikler ve döngü limitleri
    confidence_threshold: float = 0.70
    max_fact_check_retries: int = 2
    min_debate_rounds: int = 2
    max_debate_rounds: int = 3
    price_tolerance_pct: float = 1.0      # fiyat iddiası için % tolerans
    indicator_tolerance: float = 2.0      # RSI vb. için mutlak tolerans
    ratio_tolerance_pct: float = 5.0      # F/K vb. için % tolerans
    veri_yok_confidence_penalty: float = 0.10  # her eksik kaynak için güven düşüşü


settings = Settings()
