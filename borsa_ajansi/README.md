# Borsa Ajansı — Çoklu Ajanlı Analiz, Tahmin ve Sanal Portföy Sistemi

```
küresel bağlam (BIST100/30, banka, USD/TRY, Brent, altın, S&P, Nasdaq, DAX, VIX, DXY, ABD 10Y)
  + hisse verisi (yfinance/ta, temel veri, KAP/haber RSS)
   → 4 analist (paralel, hızlı model)
   → 1. Kademe Fact-Checker (programatik, en fazla 2 yeniden deneme)
   → 2. Kademe Boğa/Ayı tartışması (güçlü model, dayanaksız argüman elenir)
   → 3. Kademe Risk kapısı (güven < %70 / teknik-temel çelişkisi / R/Ö < 1.5 → BEKLE)
   → SQLite kayıt + 5/10 işlem günü performans kıyası
   → Sanal portföy: BIST30 taraması → analiz → simüle AL/SAT, stop/hedef takibi
```

## Kurulum (Windows)
```powershell
cd borsa_ajansi
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # anahtarları doldur
```

## Çalıştırma
- `baslat.bat` dosyasına çift tıkla → Telegram botu + zamanlayıcı (pencere açık kaldığı sürece çalışır).
- CLI: `.venv\Scripts\python.exe main.py analiz THYAO`
- Testler: `.venv\Scripts\python.exe -m pytest` (API'siz) · `-m network` (canlı veri) · `-m llm` (gerçek Claude)

## Telegram komutları
| Komut | Açıklama |
|---|---|
| `/analiz THYAO` | 3 kademeli tam analiz |
| `/gundem` | Piyasa + makro + KAP özeti |
| `/gecmis` | Tahminler ve 5/10 gün başarı oranı |
| `/sim_baslat 10000 14` | Sanal portföyü başlat (gerçek para yok) |
| `/portfoy` | Portföy değeri, K/Z, BIST100 kıyası |
| `/sim_islem` | Tarama + işlem döngüsünü hemen çalıştır |
| `/sim_log` | Son alım/satım olayları |
| `/sim_durdur` | Simülasyonu bitir |

Otomatik (hafta içi): 10:30 işlem döngüsü, 18:30 stop/hedef kontrolü + gün sonu raporu, (isteğe bağlı) 09:30 izleme listesi raporu.

## Sanal portföy kuralları
- Yalnızca uzun pozisyon; SAT kararı açık pozisyonu kapatır.
- Risk bazlı pozisyon: işlem başına öz sermayenin %2'si; tek pozisyon en fazla %30; en fazla 4 pozisyon.
- %0.2 komisyon + %0.1 kayma; stop ve hedef aynı gün görülürse stop sayılır; gap'te açılış fiyatı.
- Fiyat giriş bölgesinin üstündeyse kovalanmaz; AL devam ederse stop yalnızca yukarı çekilir.
- Süre dolunca tüm pozisyonlar kapanır ve BIST100'e karşı sonuç raporlanır.

## LLM sağlayıcıları
- `ANTHROPIC_API_KEY` varsa Claude (Haiku 5.5 analist, Opus 5.5 tartışma/risk), yoksa Gemini.
- `FAST_PROVIDER` / `REASONING_PROVIDER` ile katman bazında karıştırılabilir.
- Gemini: model yoğunsa (503) sıradaki modele geçer, günlük kotası biten modeli atlar.
  LLM tamamen erişilemezse sistem çökmez, güven 0 ile BEKLE döner.
- Gemini ücretsiz katmanı model başına günde ~20 istekle sınırlı; `.env`'de ekonomik ayarlar
  (`SIM_CANDIDATES=2`, `MIN_DEBATE_ROUNDS=1`, `MORNING_REPORT_ENABLED=false`) bu yüzden açık.

## Tasarım kuralları
- Tüm matematik (RSI, MA, % değişim, giriş/stop/hedef, R/Ö, pozisyon büyüklüğü, K/Z) Python'da; LLM yalnızca yorumlar.
- Ajanlar arası aktarım yalnızca Pydantic şemalarıyla (`verification/schemas.py`).
- Eksik kaynak → `"veri_yok"`; tutarsız temel veriler (ör. PD/DD > 15) otomatik ayıklanır.
- Gizli bilgiler yalnızca `.env`'de; `.env` ve `*.db` git dışı.

## Notlar
- Borsa MCP için `tools/fundamental_data.fetch_from_borsa_mcp` adaptör noktası hazır (henüz bağlı değil).
- yfinance BIST verisi ~15 dk gecikmeli olabilir; simülasyon bu fiyatları kullanır.
- `jiter==0.10.0` ve `ormsgpack==1.13.0` sabitlendi: Windows Akıllı Uygulama Denetimi bazı yeni DLL'leri engelliyor.
- Yatırım tavsiyesi değildir; sistem eğitim ve simülasyon amaçlıdır.
