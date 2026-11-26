"""Merkezi ayarlar: tüm gizli bilgiler yalnızca .env'den okunur (koda gömülmez)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _ensure_ascii_ca_bundle() -> None:
    """Windows'ta klasör yolunda Türkçe/ASCII-dışı karakter (ör. 'Masaüstü') olduğunda
    yfinance/curl_cffi libcurl katmanı certifi sertifikasını okuyamayıp SSLError üretir.
    Sertifikayı salt-ASCII TEMP dizinine kopyalayarak tüm süreci korur."""
    if os.name != "nt":
        return
    try:
        import certifi
        import shutil
        import tempfile

        src = certifi.where()
        if all(ord(ch) < 128 for ch in src):
            return
        dst = Path(tempfile.gettempdir()) / "borsa_ajansi_cacert.pem"
        if not dst.exists() or dst.stat().st_size != Path(src).stat().st_size:
            shutil.copyfile(src, dst)
        if all(ord(ch) < 128 for ch in str(dst)):
            os.environ.setdefault("CURL_CA_BUNDLE", str(dst))
            os.environ.setdefault("SSL_CERT_FILE", str(dst))
    except Exception:
        pass


_ensure_ascii_ca_bundle()

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


def _tiers(name: str, default: str) -> list[tuple[float, float, float]]:
    """'50:0.65:1.3,25:0.68:1.4,25:0.70:1.5' -> [(kümülatif_oran, min_güven, min_RÖ), ...] (0.50, 0.75, 1.00).

    Anlamı: portföyün ilk %50'si gevşek eşikle, sonraki %25'i daha sıkı, kalanı en sıkı eşikle yatırılabilir.
    """
    out, cum = [], 0.0
    for part in _csv(name, default):
        try:
            pct, conf, rr = (float(x) for x in part.split(":"))
        except ValueError:
            continue
        cum = min(1.0, cum + pct / 100)
        out.append((round(cum, 4), conf, rr))
    if not out:
        return [(1.0, 0.70, 1.5)]
    out[-1] = (1.0, out[-1][1], out[-1][2])  # son kademe her zaman %100'e kadar
    return out


_DEFAULT_TIERS = "50:0.65:1.3,25:0.68:1.4,25:0.70:1.5"


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
    # Kademeli yatırım: portföyün ne kadarının hangi güven / risk-ödül eşiğiyle yatırılabileceği
    sim_tiers: list[tuple[float, float, float]] = field(default_factory=lambda: _tiers("SIM_TIERS", _DEFAULT_TIERS))
    # Daha iyi adaya geçiş: yeni aday, eldeki en zayıf pozisyondan en az bu kadar daha güvenilir VE
    # kalan risk/ödülü bu kat daha yüksek olmalı (komisyon + gereksiz al-sat önlemi)
    sim_rotation_conf_margin: float = field(default_factory=lambda: float(os.getenv("SIM_ROTATION_CONF_MARGIN", "0.05")))
    sim_rotation_rr_factor: float = field(default_factory=lambda: float(os.getenv("SIM_ROTATION_RR_FACTOR", "1.25")))
    sim_risk_per_trade_pct: float = field(default_factory=lambda: float(os.getenv("SIM_RISK_PER_TRADE_PCT", "2")))
    sim_max_position_pct: float = field(default_factory=lambda: float(os.getenv("SIM_MAX_POSITION_PCT", "30")))
    sim_commission_pct: float = field(default_factory=lambda: float(os.getenv("SIM_COMMISSION_PCT", "0.2")))
    sim_slippage_pct: float = field(default_factory=lambda: float(os.getenv("SIM_SLIPPAGE_PCT", "0.1")))
    sim_trade_hour: int = field(default_factory=lambda: int(os.getenv("SIM_TRADE_HOUR", "10")))
    sim_trade_minute: int = field(default_factory=lambda: int(os.getenv("SIM_TRADE_MINUTE", "30")))
    sim_eod_hour: int = field(default_factory=lambda: int(os.getenv("SIM_EOD_HOUR", "18")))
    sim_eod_minute: int = field(default_factory=lambda: int(os.getenv("SIM_EOD_MINUTE", "30")))

    # KAP bildirim metinleri + faaliyet raporu/sunum okuma
    documents_enabled: bool = field(default_factory=lambda: os.getenv("DOCUMENTS_ENABLED", "true").lower() == "true")
    # false -> KAP ve PDF metinleri deterministik Python kurallarıyla özetlenir (0 LLM çağrısı!)
    documents_use_llm: bool = field(default_factory=lambda: os.getenv("DOCUMENTS_USE_LLM", "true").lower() == "true")

    # ---- Yerel Veri Ambarı (3 yıllık BIST100 + makro veri seti, 0 LLM) ----
    warehouse_db_path: str = field(default_factory=lambda: os.getenv("WAREHOUSE_DB_PATH", str(BASE_DIR / "data" / "market_warehouse.db")))
    warehouse_csv_path: str = field(default_factory=lambda: os.getenv("WAREHOUSE_CSV_PATH", str(BASE_DIR / "data" / "bist100_3y_daily.csv")))
    use_local_warehouse: bool = field(default_factory=lambda: os.getenv("USE_LOCAL_WAREHOUSE", "true").lower() == "true")
    data_sync_enabled: bool = field(default_factory=lambda: os.getenv("DATA_SYNC_ENABLED", "true").lower() == "true")
    data_sync_hour: int = field(default_factory=lambda: int(os.getenv("DATA_SYNC_HOUR", "18")))
    data_sync_minute: int = field(default_factory=lambda: int(os.getenv("DATA_SYNC_MINUTE", "45")))

    # ---- LLM Kota Koruma & Akıllı Ön Eleme ----
    # Aynı hisse+bar için önbellek süresi (sn); 0 = kapalı (ör. 14400 = 4 saat boyunca aynı analizi tekrar üretmez)
    analysis_cache_ttl_s: float = field(default_factory=lambda: float(os.getenv("ANALYSIS_CACHE_TTL_S", "0")))
    # true -> 4 analist tek birleşik yapılandırılmış LLM çağrısında çalışır (%75 analist kotası tasarrufu)
    economy_single_call: bool = field(default_factory=lambda: os.getenv("ECONOMY_SINGLE_CALL", "false").lower() == "true")
    # true -> açık pozisyonda anlamlı fiyat/teknik değişim yoksa sabah yeniden LLM çağrılmaz
    sim_skip_unchanged_reanalysis: bool = field(default_factory=lambda: os.getenv("SIM_SKIP_UNCHANGED_REANALYSIS", "false").lower() == "true")
    # true -> taramada R/Ö kapısından geçemeyecek adaylar LLM'e hiç gönderilmez
    screen_prefilter_rr: bool = field(default_factory=lambda: os.getenv("SCREEN_PREFILTER_RR", "false").lower() == "true")
    # true -> tarama kontenjanına aşırı satım dönüş / KAP katalizör adaylarını da dahil eder
    screen_include_reversals: bool = field(default_factory=lambda: os.getenv("SCREEN_INCLUDE_REVERSALS", "false").lower() == "true")

    # ---- Gelişmiş Simülasyon & Risk Kalibrasyonu ----
    # Boşta bekleyen nakde günlük Para Piyasası Fonu (PPF) getirisi (ör. 0.10 = günde %0.10 ~ yıllık %44 bileşik)
    sim_cash_daily_yield_pct: float = field(default_factory=lambda: float(os.getenv("SIM_CASH_DAILY_YIELD_PCT", "0.0")))
    # Kâr 1.5R'ye ulaştığında stop-loss otomatik başabaş (giriş) seviyesine çekilir; 0 = kapalı
    sim_breakeven_r: float = field(default_factory=lambda: float(os.getenv("SIM_BREAKEVEN_R", "0.0")))
    # Giriş bölgesi üst sınır toleransı (%) — gecikmeli veride kaliteli sinyali kaçırmamak için
    sim_entry_chase_pct: float = field(default_factory=lambda: float(os.getenv("SIM_ENTRY_CHASE_PCT", "1.0")))
    # Piyasa risk-off / BIST defansif rejimdeyken işlem başı risk çarpanı (ör. 0.5 -> %2 yerine %1 risk)
    sim_defensive_risk_mult: float = field(default_factory=lambda: float(os.getenv("SIM_DEFENSIVE_RISK_MULT", "1.0")))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(BASE_DIR / "borsa_ajansi.db")))

    # Eşikler ve döngü limitleri
    # AL/SAT için en gevşek kademe eşikleri (risk yöneticisi kapısı); kademe kontrolü simülasyonda yapılır
    confidence_threshold: float = field(default_factory=lambda: min(t[1] for t in _tiers("SIM_TIERS", _DEFAULT_TIERS)))
    min_risk_reward: float = field(default_factory=lambda: min(t[2] for t in _tiers("SIM_TIERS", _DEFAULT_TIERS)))
    max_fact_check_retries: int = 2
    min_debate_rounds: int = field(default_factory=lambda: int(os.getenv("MIN_DEBATE_ROUNDS", "2")))
    max_debate_rounds: int = field(default_factory=lambda: int(os.getenv("MAX_DEBATE_ROUNDS", "3")))
    price_tolerance_pct: float = 1.0      # fiyat iddiası için % tolerans
    indicator_tolerance: float = 2.0      # RSI vb. için mutlak tolerans
    ratio_tolerance_pct: float = 5.0      # F/K vb. için % tolerans
    veri_yok_confidence_penalty: float = 0.10  # her eksik kaynak için güven düşüşü


settings = Settings()
