# Borsa Ajansı — Çoklu Ajanlı Analiz ve Tahmin Sistemi

```
veri (yfinance/ta, RSS) → 4 analist (paralel, FAST_MODEL)
   → 1. Kademe Fact-Checker (programatik, en fazla 2 yeniden deneme)
   → 2. Kademe Boğa/Ayı tartışması (REASONING_MODEL, 2–3 tur, dayanaksız argüman elenir)
   → 3. Kademe Risk kapısı (güven < %70 / teknik-temel çelişkisi / R/Ö < 1.5 → BEKLE)
   → SQLite kayıt + 5/10 işlem günü performans kıyası
```

## Kurulum (Windows)
```powershell
cd borsa_ajansi
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_ID doldur
```

## Kullanım
```powershell
.venv\Scripts\python.exe main.py analiz THYAO.IS   # tek hisse CLI
.venv\Scripts\python.exe main.py bot               # Telegram + hafta içi 09:30 rapor
.venv\Scripts\python.exe -m pytest                 # birim/entegrasyon testleri (API'siz)
.venv\Scripts\python.exe -m pytest -m network      # canlı veri testleri
.venv\Scripts\python.exe -m pytest -m llm          # gerçek Claude testi (API anahtarı gerekir)
```

Telegram komutları: `/analiz <HİSSE>`, `/gundem`, `/gecmis`.

## Tasarım kuralları
- Tüm matematik (RSI, MA, % değişim, giriş/stop/hedef, R/Ö) Python'da; LLM yalnızca yorumlar.
- Ajanlar arası aktarım yalnızca Pydantic şemalarıyla (`verification/schemas.py`), `with_structured_output`.
- Eksik kaynak → `"veri_yok"`; her eksik/doğrulanamayan analist güveni 0.10 düşürür.
- Gizli bilgiler yalnızca `.env`'de; `.env` ve `*.db` git dışı.

## Notlar
- Borsa MCP için `tools/fundamental_data.fetch_from_borsa_mcp` adaptör noktası bırakıldı (henüz bağlı değil); temel veri şimdilik yfinance.
- KAP'ın resmi açık API'si olmadığından KAP başlıkları Google News RSS üzerinden aranır.
- `jiter==0.10.0` ve `ormsgpack==1.13.0` sabitlendi: Windows Akıllı Uygulama Denetimi bazı yeni sürüm DLL'lerini engelliyor.
