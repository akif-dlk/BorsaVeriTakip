# PROJE İŞ PLANI: Çoklu Ajanlı Borsa Analiz ve Tahmin Sistemi

## 1. Genel Mimari ve Teknoloji Yığını
Bu proje; BIST ve global piyasaları analiz eden, ajanlar arası 3 kademeli doğrulama yapan ve Telegram üzerinden kontrol edilen modüler bir Python sistemidir.
- **Orkestrasyon:** LangGraph (StateGraph tabanlı deterministik akış)
- **LLM Katmanı:** Anthropic Claude API (Katmanlı kullanım: Veri özetleme/temizleme için hızlı model, tartışma ve risk kararı için güçlü akıl yürütme modeli)
- **Veri Katmanı:** Borsa MCP (BIST/KAP/EVDS verileri için) + `yfinance` / `ta` (teknik indikatörler) + RSS/NewsAPI (gündem)
- **Hafıza:** SQLite (geçmiş tahminler, doğrulama logları ve 5/10 günlük performans kıyaslaması)
- **İletişim Arayüzü:** `python-telegram-bot` (asenkron) + `APScheduler` (seans öncesi otomatik raporlama). n8n KULLANILMAYACAK.

---

## 2. Hedef Klasör Yapısı
```text
borsa_ajansi/
├── config/
│   ├── settings.py          # .env yükleyici, model seçimleri, eşik değerleri
│   └── prompts.py           # Her ajan için sistem promptları
├── tools/
│   ├── market_data.py       # Fiyat, hacim, teknik indikatör hesaplama (pandas/ta)
│   ├── fundamental_data.py  # Bilanço, F/K, FAVÖK, Borsa MCP entegrasyonu
│   └── news_sentiment.py    # KAP bildirimleri, haber RSS ve sosyal medya/duygu verisi
├── agents/
│   ├── analysts.py          # Teknik, Temel, Haber, Duygu ajanları (Paralel çalışır)
│   ├── debate.py            # Boğa (Bull) ve Ayı (Bear) ajanları
│   └── risk_manager.py      # Nihai karar ve pozisyon/stop-loss yöneticisi
├── verification/
│   ├── fact_checker.py      # 1. Kademe: Veri ve halüsinasyon doğrulayıcı
│   └── schemas.py           # Pydantic JSON şemaları (ajanlar arası kesin sözleşme)
├── graph/
│   └── workflow.py          # LangGraph akışı, döngü limitleri ve yönlendirme
├── memory/
│   └── db.py                # SQLite tahmin kaydı ve geçmiş performans takibi
├── telegram_bot/
│   └── bot.py               # Telegram komutları ve zamanlanmış bildirimler
├── tests/                   # Her faz için birim ve entegrasyon testleri
├── .env.example
└── main.py