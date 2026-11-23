"""Ajanlar arası kesin sözleşme. Ajanlar düz metin değil, yalnızca bu modelleri üretir/tüketir."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AnalystName = Literal["teknik", "temel", "gundem", "duygu"]
Signal = Literal["olumlu", "olumsuz", "notr"]
Decision = Literal["AL", "SAT", "BEKLE"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NumericClaim(_Strict):
    """Analistin raporunda dayandığı her sayısal iddia. `field`, ham verideki anahtarın birebir adıdır."""
    field: str = Field(description="Ham veri sözlüğündeki anahtar adı, ör. 'rsi14', 'close', 'pe_ratio', 'levels.support_20d'")
    value: float = Field(description="Ham veriden aynen kopyalanan sayı (hesaplama yapma)")


# ---------- Katman 1: Analist çıktısı (LLM) ----------
class AnalystReport(_Strict):
    analyst: AnalystName
    ticker: str
    data_date: str = Field(description="Verinin tarihi (YYYY-MM-DD) ham veriden aynen; yoksa 'veri_yok'")
    signal: Signal
    score: float = Field(ge=-1.0, le=1.0, description="-1 (çok olumsuz) .. +1 (çok olumlu) nitel değerlendirme")
    confidence: float = Field(ge=0.0, le=1.0)
    key_points: list[str] = Field(description="En fazla 5 kısa madde")
    claims: list[NumericClaim] = Field(description="Metinde kullanılan TÜM sayısal değerler")
    data_gaps: list[str] = Field(description="'veri_yok' olan ve analizi etkileyen alanlar")
    summary: str


# ---------- Katman 1: Fact-check sonucu (Python) ----------
class ClaimCheck(_Strict):
    field: str
    claimed: float
    actual: float | str | None
    ok: bool
    reason: str = ""


class FactCheckResult(_Strict):
    analyst: AnalystName
    passed: bool
    checks: list[ClaimCheck] = []
    errors: list[str] = []
    attempts: int = 1


# ---------- Katman 2: Boğa / Ayı tartışması ----------
class Argument(_Strict):
    claim: str = Field(description="Tek cümlelik argüman")
    source_analyst: AnalystName = Field(description="Dayanak olan analist raporu")
    evidence_field: str = Field(description="Dayanak verinin ham veri anahtarı, ör. 'rsi14'; sayısal değilse 'key_points'")
    evidence_value: float | None = Field(description="Ham veriden aynen kopyalanan sayı; sayısal değilse null")


class DebateTurn(_Strict):
    side: Literal["boga", "ayi"]
    round: int
    thesis: str
    arguments: list[Argument]
    rebuttals: list[str] = Field(description="Karşı tarafın önceki argümanlarına somut veriyle çürütmeler")


class ValidatedTurn(_Strict):
    turn: DebateTurn
    accepted: list[Argument]
    rejected: list[str]  # elenen argüman + gerekçe


# ---------- Katman 3: Risk Yöneticisi ----------
class RiskJudgement(_Strict):
    """LLM'in (hakem) ürettiği nitel karar. Seviyeler ve R/Ö Python'da hesaplanır."""
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    winning_side: Literal["boga", "ayi", "berabere"]
    technical_fundamental_conflict: bool
    rationale: str
    key_risks: list[str]


class TradePlan(_Strict):
    entry_low: float
    entry_high: float
    stop_loss: float
    target: float
    risk_reward: float
    basis: str


class FinalDecision(_Strict):
    ticker: str
    data_date: str
    decision: Decision
    llm_decision: Decision
    raw_confidence: float
    confidence: float
    winning_side: str
    rationale: str
    key_risks: list[str]
    gate_reasons: list[str] = []  # BEKLE'ye çekilme / güven düşürme gerekçeleri
    trade_plan: TradePlan | None = None
    close: float | None = None


# ---------- Belge okuma: KAP bildirimleri ve faaliyet raporu / yatırımcı sunumu (LLM, hızlı model) ----------
class DisclosureNote(_Strict):
    disclosure_index: int = Field(description="Bildirimin KAP numarası (verilen listeden aynen)")
    impact: Signal = Field(description="Hisse fiyatına olası etki")
    materiality: Literal["yuksek", "orta", "dusuk"]
    summary: str = Field(description="En fazla 2 cümle; yalnızca bildirim metninde yazanlar")
    evidence_quote: str = Field(description="Bildirim metninden AYNEN kopyalanmış kısa alıntı (en fazla 200 karakter)")


class DisclosureDigest(_Strict):
    notes: list[DisclosureNote] = Field(description="Her bildirim için bir not (önemsiz rutin bildirimler atlanabilir)")


class OutlookItem(_Strict):
    category: Literal["hedef_beklenti", "yatirim_kapasite", "strateji", "risk"]
    statement: str = Field(description="Şirketin geleceğe dönük ifadesinin kısa Türkçe özeti (1 cümle)")
    horizon: str = Field(description="Zaman ufku, metinde yazıyorsa (ör. '2027', '2033-2037'); yoksa 'belirtilmemiş'")
    source: str = Field(description="Belge adı (verilen listeden aynen)")
    evidence_quote: str = Field(description="Belge metninden AYNEN kopyalanmış alıntı (en fazla 250 karakter)")


class OutlookDigest(_Strict):
    items: list[OutlookItem] = Field(description="En fazla 10 madde; önemli geleceğe dönük planlar/hedefler/riskler")
    summary: str = Field(description="Şirketin yönetim bakışı ve gelecek planlarının en fazla 3 cümlelik özeti")
