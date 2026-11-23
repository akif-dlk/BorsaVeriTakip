"""Analiz sonucunu okunabilir metne çevirir (CLI ve Telegram ortak). Sunum katmanıdır; ajanlar arası aktarım değildir."""
from __future__ import annotations

from typing import Any

from verification.schemas import FinalDecision

EMOJI = {"AL": "🟢", "SAT": "🔴", "BEKLE": "🟡"}
SIG = {"olumlu": "▲", "olumsuz": "▼", "notr": "■"}


IMPACT = {"olumlu": "▲", "olumsuz": "▼", "notr": "■"}


def _document_lines(bundle: dict[str, Any]) -> list[str]:
    docs = bundle.get("documents")
    fin = bundle.get("financials") or {}
    out: list[str] = []
    if fin.get("status") == "ok":
        def p(k):
            v = fin.get(k)
            return f"{v:+.1f}%" if isinstance(v, (int, float)) else "veri_yok"
        out += ["", f"📊 Son çeyrek ({fin['latest_quarter']}): satış yıllık {p('revenue_yoy_pct')}, "
                    f"net kâr yıllık {p('net_income_yoy_pct')}, net marj değişimi "
                    + (f"{fin['net_margin_change_pts']:+.1f} puan" if isinstance(fin.get('net_margin_change_pts'), (int, float))
                       else "veri_yok")]
    if not docs:
        return out
    k, o = docs.get("kap") or {}, docs.get("outlook") or {}
    out += ["", "📄 Belgeler"]
    if k.get("status") == "ok":
        notes = k.get("notes", [])
        out.append(f"KAP: {k.get('disclosure_count', 0)} bildirim okundu, {len(notes)} doğrulanmış not"
                   + (f", {k['rejected_notes']} doğrulanamayan atıldı" if k.get("rejected_notes") else ""))
        for n in sorted(notes, key=lambda n: {"yuksek": 0, "orta": 1, "dusuk": 2}[n["materiality"]])[:3]:
            out.append(f"  {IMPACT[n['impact']]} {n['date'][:10]} {n['title']}: {n['summary']}")
    else:
        out.append(f"KAP: veri_yok ({k.get('reason', '-')})")
    if o.get("status") == "ok":
        names = ", ".join(d["name"] for d in o.get("documents", []))
        out.append(f"Rapor/sunum: {names} → {len(o.get('items', []))} gelecek planı maddesi")
        for it in o.get("items", [])[:3]:
            out.append(f"  • [{it['horizon']}] {it['statement']}")
    else:
        out.append(f"Rapor/sunum: veri_yok ({o.get('reason', '-')})")
    return out


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
    lines += _document_lines(state.get("bundle", {}))
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
