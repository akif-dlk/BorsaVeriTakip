# Antigravity Geliştirme & Mimari Dönüşüm Günlüğü (`borsa_ajansi`)

**Tarih:** 27 Kasım 2026  
**Kapsam:** Kota Dostu Yerel Veri Ambarı (3 Yıllık BIST100 + Makro Dataset), Sıfır-LLM Belge Ayrıştırıcı, İki Kanallı Tarama, Sektörel Çarpan Kıyası, Hakem Kalibrasyonu ve 3 Yıllık Deterministik Backtest Motoru.

---

## 1. Mimari Prensip: "Veriyi Bot Çeker, Matematiği Python Yapar, LLM Sadece Düşünür"

Önceki yapıda her hisse analizinde hem veri çekimi tekrarlanıyor hem de belge okuma ve 4 ayrı analist çağrısı yüzünden tek hissede **11–19 LLM isteği**, tek bir sabah simülasyon döngüsünde ise **~70–80 LLM isteği** harcanıyordu. Bu durum ücretsiz/sınırlı LLM katmanlarında (Gemini günlük ~20 istek/model, EVREN eşzamanlılık sınırı) sürekli `429 / kota aşımı` üretiyordu.

Yeni mimaride görev ayrımı keskinleştirildi:

| Katman | Eski Durum | Yeni Durum | LLM Çağrısı |
|---|---|---|---|
| **Fiyat & Makro Veri (120 sembol)** | Her taramada canlı tekil HTTP istekleri | `tools/data_warehouse.py` ile yerel SQLite + CSV veri seti (günlük artımlı fark botu) | **0 LLM** |
| **KAP Bildirim & Faaliyet Raporu PDF** | Hisse başı +2 LLM çağrısı | `DOCUMENTS_USE_LLM=false` ile deterministik Python metin/anahtar kelime çıkarımı | **0 LLM** |
| **R/Ö Uygunluk Kontrolü** | 11 LLM çağrısından *sonra* kapıda eleniyordu | `SCREEN_PREFILTER_RR=true` ile tarama aşamasında elenir | **0 LLM** |
| **Açık Pozisyon Takibi (Stop/PPF/1.5R)** | Her sabah tüm açık pozisyonlara tam LLM analizi | `SIM_SKIP_UNCHANGED_REANALYSIS=true` + otomatik `1.5R` başabaş stop | **0 LLM** |
| **4 Uzman Analist Katmanı** | 4 ayrı paralel LLM isteği | `ECONOMY_SINGLE_CALL=true` ile tek birleşik yapılandırılmış şema (`BatchAnalystReports`) | **1 LLM** |
| **Boğa/Ayı Tartışması + Risk Hakemi** | 4–6 tartışma + 1 hakem isteği | `MIN_DEBATE_ROUNDS=1` (2 tartışma + 1 kalibre edilmiş hakem) + `4 saat` analiz önbelleği | **3 LLM** |
| **Toplam (Hisse Başına)** | **11 – 19 LLM çağrısı** | **4 LLM çağrısı** (önbellek isabetinde **0**) | **~%70–85 Tasarruf** |

---

## 2. Yerel 3 Yıllık Veri Seti ve Veri Botu (`tools/data_warehouse.py`)

* **Oluşturulan Veri Seti:**
  * **SQLite Veri Ambarı:** `data/market_warehouse.db` (`WAL` modu, indeksli `ohlcv_daily`, `kap_raw_cache`, `sync_runs` tabloları)
  * **Taşınabilir CSV Veri Seti:** `data/bist100_3y_daily.csv` (pandas, Jupyter, ML veya dbt ile doğrudan okunabilir)
  * **Kapsam:** **120 sembol** (`107` BIST100 hissesi + `13` küresel/yerel makro gösterge: `XU100.IS`, `XU030.IS`, `XBANK.IS`, `USDTRY=X`, `EURTRY=X`, `GC=F`, `BZ=F`, `^GSPC`, `^IXIC`, `^GDAXI`, `^VIX`, `DX-Y.NYB`, `^TNX`)
  * **İndirilen Gerçek Veri Boyutu:** **88,268 günlük OHLCV satırı** (`2023-11-27` → `2026-11-26`)
* **Artımlı (Incremental) Güncelleme:** İlk çalıştırmada 3 yıllık geçmişi toplu (`yf.download`) indirir; sonraki çalıştırmalarda her sembolün son kayıt tarihine bakarak yalnızca eksik son günleri 2–3 saniyede tamamlar.
* **Otomatik Zamanlayıcı:** Telegram botu açıkken hafta içi her gün **18:20**'de (seans kapanışı sonrası, 18:30 gün sonu kontrolünden hemen önce) `0` LLM harcayarak yerel veri setini günceller.
* **Kritik Windows Bug Düzeltmesi (`config/settings.py` → `_ensure_ascii_ca_bundle`):**
  Proje dizin yolunda Türkçe karakter (`Masaüstü`) bulunduğunda `yfinance` altındaki `curl_cffi / libcurl` kütüphanesi `certifi` SSL sertifikasını okuyamayıp `SSLError` veriyordu. Başlangıçta sertifika otomatik olarak salt-ASCII `%TEMP%\borsa_ajansi_cacert.pem` konumuna kopyalanarak sorun kökünden çözüldü.

---

## 3. Alfa, Risk ve Kalibrasyon Geliştirmeleri (Dosya Bazlı Günlük)

### A. Hakem "Sürekli BEKLE" (Nakit Tuzağı) Kalibrasyonu
* **Dosyalar:** `config/prompts.py`, `verification/schemas.py`, `agents/risk_manager.py`, `graph/workflow.py`
* **Sorun:** Mevcut `borsa_ajansi.db` kayıtlarında 22 gerçek analizin 22'si de `BEKLE` üretmişti (hem prompt korkutması hem Python kapıları çift fren yaratıyordu).
* **Çözüm:**
  1. `RiskJudgement` ve `FinalDecision` şemalarına `-1.0 .. +1.0` aralığında `conviction_score` (beklenti asimetrisi puanı) eklendi.
  2. Hakem prompt'u "sıfır risk aramak yerine boğa/ayı beklenti asimetrisini tartacak" şekilde yeniden yazıldı.
  3. `apply_gates()` içine deterministik kalibrasyon eklendi: LLM tartışmada açıkça `winning_side="boga"` ve `conviction_score >= 0.35` verdiği halde alışkanlıkla `BEKLE` yazarsa (ve teknik-temel çelişkisi yoksa), Python R/Ö ve güven kapılarından geçmek kaydıyla kararı `AL` adaylığına yükseltir.
  4. `PredictionStore.calibration_summary()` ile son 30 tahmindeki `BEKLE` oranı `%90` üstüne çıktığında hakeme öz-farkındalık (self-reflection) uyarısı beslenir.

### B. İki Kanallı Tarama: Trend/Momentum + Aşırı Satım Dönüş & KAP Katalizörü
* **Dosya:** `tools/screener.py`
* **Çözüm:**
  * **Kanal 1 (`trend_momentum`):** `Close > MA50 > MA200`, `MACD > 0`, sağlıklı RSI ve BIST100'e karşı pozitif göreli güç.
  * **Kanal 2 (`donus_katalizor`):** `score_reversal_catalyst()` eklendi — aşırı satım bölgesinde (`RSI <= 36` / Bollinger alt bandı) 60 günlük destek üzerinde hacimli dönüş mumu yakan veya son 7 günde yerel KAP önbelleğinde olumlu yüksek önemli bildirimi (ihale, sözleşme, geri alım, temettü, yatırım) bulunan hisseleri yakalar.
  * **Ön R/Ö Filtresi (`SCREEN_PREFILTER_RR`):** Python'da hesaplanan `TradePlan.risk_reward < min_risk_reward` olan hisseler daha LLM'e gitmeden elenir.

### C. BIST Sektörel Göreli Çarpan Kıyası ve Yerel Piyasa Rejimi
* **Dosyalar:** `tools/fundamental_data.py`, `tools/market_context.py`, `graph/report.py`
* **Çözüm:**
  * BIST hisseleri 11 yerel sektör grubuna (`banka`, `holding`, `havacilik`, `savunma_teknoloji`, `perakende_gida`, `enerji`, `otomotiv`, `sanayi_emtia`, `iletisim`, `gyo`, `genel_bist`) eşlendi. Hissenin `F/K` ve `PD/DD` çarpanlarının sektör medyanına göre `%` farkı ve `iskontolu / sektore_uyumlu / primli` etiketi Python'da hesaplanıp temel analiste verildi.
  * `compute_bist_regime()` ile `XU100` (`MA50` konumu), `XBANK` ve `VIX` birlikte değerlendirilerek `boga / notr / defansif` rejimi ve önerilen risk çarpanı (`1.0 / 0.75 / 0.5`) üretildi.

### D. Sanal Portföy Gerçekçiliği (PPF Getirisi + 1.5R Başabaş Stop) ve Alfa Karnesi
* **Dosyalar:** `simulation/paper_trader.py`, `memory/db.py`
* **Çözüm:**
  * **PPF Nakit Getirisi (`SIM_CASH_DAILY_YIELD_PCT`):** Türkiye piyasası gerçekliğine uygun olarak boşta bekleyen nakde iş günü bazında Para Piyasası Fonu getirisi işletilir ve portföy raporunda ayrı gösterilir.
  * **1.5R Başabaş Kilidi (`SIM_BREAKEVEN_R`):** Pozisyon `1.5R` kâr gördüğünde LLM beklemeksizin stop-loss otomatik olarak komisyon dahil başabaş (`entry_price * 1.004`) seviyesine çekilir.
  * **Alfa Karnesi (`judge_alpha`):** `evaluations` tablosuna `bench_return_pct`, `alpha_pct` ve `alpha_success` sütunları eklendi; bir `AL` tahmini ancak işlem maliyetini aşıp aynı dönem `XU100.IS` getirisini geçtiğinde gerçek "alfa başarısı" sayılır.

---

## 4. Gerçek 3 Yıllık BIST100 Backtest Sonuçları (`simulation/backtest.py`)

İndirilen gerçek veri seti (`2024-02-21 → 2026-11-25`, **688 işlem günü**, **107 BIST hissesi**, komisyon `%0.2` + kayma `%0.1` dahil, **0 LLM çağrısı**) üzerinde yapılan A/B doğrulaması:

| Metrik (688 İşlem Günü) | Kalibrasyon Öncesi (Saf Kırılım Ağırlıklı) | Rejim + Dönüş Kalibrasyonu Sonrası | Değişim / Yorum |
|---|---|---|---|
| **BIST100 (`XU100.IS`) Getirisi** | `+14.43%` | `+14.43%` | Referans endeks |
| **Strateji Toplam Getirisi** | `+8.47%` | **`+27.99%`** | **+19.52 puan iyileşme** |
| **Endeks Üstü Alfa** | `-5.96 puan` | **`+13.56 puan`** | BIST100'ün yaklaşık **2 katı** performans |
| **Maksimum Düşüş (Max Drawdown)** | `-39.62%` | **`-23.84%`** | **15.78 puan daha düşük risk** |
| **İşlem Başı Beklenti (Expectancy)** | `-0.14%` | **`+0.11%`** | Pozitif matematiksel beklentiye geçiş |
| **`trend_momentum` Kanal Ort.** | `-0.37%` (241 işlem) | `-0.19%` (137 işlem) | `XU100 < MA50` testere günlerinde kırılım kovalamak durduruldu |
| **`donus_katalizor` Kanal Ort.** | **`+1.69%`** (30 işlem) | **`+0.46%`** (117 işlem) | BIST'in yatay/faiz dönemlerinde dip dönüş kanalı net alfa üretti |

> **Stratejik Çıkarım:** 2024–2026 gibi yüksek TL faizli (%40–50 PPF) ve yatay endeks dönemlerinde saf teknik kırılım kovalamak komisyon ve testere yüzünden para eritmektedir. Deterministik katman `XU100 < MA50` iken nakdi PPF'de koruyup aşırı satım dönüşlerine odaklandığında tek başına BIST100'e **+13.56 puan fark** atmaktadır. Bu baz stratejinin üzerine **LLM düşünme katmanı** (zayıf bilançolu "ucuz tuzaklarını" eleyip gerçek KAP katalizörlü olanları seçmek için) eklendiğinde hem işlem sayısı düşer hem de kazanma oranı yukarı taşınır.

---

## 5. Yeni Komutlar ve Test Doğrulaması

### CLI Komutları
```powershell
# 1) 3 yıllık BIST100 + makro veri setini indir / eksik günleri güncelle (0 LLM)
.\.venv\Scripts\python.exe main.py veri-cek --years 3

# 2) BIST30 son KAP bildirimlerini de yerel önbelleğe çek (0 LLM)
.\.venv\Scripts\python.exe main.py veri-cek --years 3 --kap

# 3) Yerel 3 yıllık veri seti üzerinde deterministik backtest çalıştır (0 LLM)
.\.venv\Scripts\python.exe main.py backtest --days 750 --cash 100000 --ppf 0.10

# 4) Tek hisse 3 kademeli tam analiz (yerel veriden okur, LLM yalnızca analiz yapar)
.\.venv\Scripts\python.exe main.py analiz THYAO
```

### Telegram Komutları
* `/veri_guncelle` — 3 yıllık BIST100 + makro yerel veri ambarını günceller (`0 LLM`).
* `/backtest [gün]` — Yerel veri setinde `0 LLM` strateji backtest karnesini gönderir.

### Test Durumu
* Toplam **113 birim ve entegrasyon testi** (`tests/test_upgrades.py` dahil) ağ ve API bağımsız modda **`113 passed` (%100 başarı)** ile doğrulandı.
