"""Analiz sonucunu okunabilir metne çevirir (CLI ve Telegram ortak). Sunum katmanıdır; ajanlar arası aktarım değildir."""
from __future__ import annotations

from typing import Any

from verification.schemas import FinalDecision

EMOJI = {"AL": "🟢", "SAT": "🔴", "BEKLE": "🟡"}
SIG = {"olumlu": "▲", "olumsuz": "▼", "notr": "■"}


def format_report(state: dict[str, Any]) -> str:
    f: FinalDecision = state["final"]
    lines = [
        f"{EMOJI[f.decision]} {f.ticker} — KARAR: {f.decision}",
        f"Tarih: {f.data_date} | Kapanış: {f.close if f.close is not None else 'veri_yok'}",
        f"Güven: %{f.confidence * 100:.0f} (ham %{f.raw_confidence * 100:.0f}) | Hakem: {f.llm_decision}, kazanan taraf: {f.winning_side}",
    ]
    if f.trade_plan:
        p = f.trade_plan
        lines += [
            "",
            "📌 İşlem Planı (Python ile hesaplandı)",
            f"Giriş bölgesi: {p.entry_low} – {p.entry_high}",
            f"Stop-loss: {p.stop_loss}",
            f"Hedef: {p.target}",
            f"Risk/Ödül: {p.risk_reward}",
            f"Dayanak: {p.basis}",
        ]
    lines += ["", "🧠 Analistler"]
    for name, r in state.get("reports", {}).items():
        chk = state.get("checks", {}).get(name)
        ok = "✅" if chk and chk.passed else "⚠️"
        att = f" ({chk.attempts}. deneme)" if chk and chk.attempts > 1 else ""
        lines.append(f"{ok} {name}: {SIG[r.signal]} {r.signal} (skor {r.score:+.2f}, güven %{r.confidence * 100:.0f}){att}")
        if r.key_points:
            lines.append("   • " + r.key_points[0])
    deb = state.get("debate", [])
    if deb:
        acc = sum(len(v.accepted) for v in deb)
        rej = sum(len(v.rejected) for v in deb)
        lines += ["", f"🐂🐻 Tartışma: {state.get('round', 0)} tur, {acc} argüman kabul, {rej} elendi"]
        for side in ("boga", "ayi"):
            last = next((v for v in reversed(deb) if v.turn.side == side), None)
            if last:
                lines.append(f"{'🐂' if side == 'boga' else '🐻'} {last.turn.thesis}")
    lines += ["", "⚖️ Gerekçe", f.rationale]
    if f.key_risks:
        lines += ["", "⚠️ Riskler"] + [f"• {k}" for k in f.key_risks]
    if f.gate_reasons:
        lines += ["", "🚦 Kapı notları"] + [f"• {g}" for g in f.gate_reasons]
    lines += ["", "ℹ️ Bu rapor otomatik üretilmiştir, yatırım tavsiyesi değildir."]
    return "\n".join(lines)
