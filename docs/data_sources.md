# Veri kaynakları ve kullanım koşulları

| Veri | Kaynak | Sembol | Anahtar | Not |
|---|---|---|---|---|
| USD/TRY, EUR/TRY | yfinance (Yahoo Finance) | `USDTRY=X`, `EURTRY=X` | Yok | Varsayılan kaynak |
| USD/TRY, EUR/TRY | TCMB EVDS (opsiyonel) | `TP.DK.USD.A`, `TP.DK.EUR.A` | Ücretsiz, kayıt gerekir | Resmî gösterge kur; `EVDS_API_KEY` doluysa kullanılır |
| BIST hisseleri | yfinance | `THYAO.IS`, `GARAN.IS`, ... | Yok | ~15 dk gecikmeli |
| BIST 100 | yfinance | `XU100.IS` | Yok | |
| Ons altın | yfinance | `GC=F` | Yok | COMEX vadeli, USD/ons |
| Gram altın | Hesaplama | — | — | `GC=F × USDTRY / 31.1035` |

## Kullanım koşulları ve limitler

- **yfinance** resmî bir API değildir; Yahoo Finance'in herkese açık uç noktalarını kullanır. Yahoo'nun
  kullanım koşulları verinin **kişisel/eğitim amaçlı** kullanımına izin verir, ticari yeniden dağıtıma izin vermez.
  Bu proje portföy/eğitim amaçlıdır. Belgelenmiş bir limit yoktur; çok sık istekte geçici engel (HTTP 429)
  gelebilir. Bu yüzden günlük veri tek seferde toplu (`yf.download`) çekilir, hata alınırsa üstel bekleme ile
  yeniden denenir ve dakikalık akış (Hafta 3) en fazla dakikada bir istekle sınırlanır.
- **TCMB EVDS** ücretsizdir, kayıt ve API anahtarı gerektirir. Anahtar `.env` içinde tutulur, repoya girmez.
- Veriler yatırım tavsiyesi değildir.

## Ham tablolar

| Tablo | Anahtar | Doldurulan |
|---|---|---|
| `raw.fx_rates` | `(pair, rate_date, source)` | `bvt.fetch_fx` (yfinance OHLC; EVDS yalnızca `close`) |
| `raw.stock_prices` | `(symbol, price_date, source)` | `bvt.fetch_stocks` (yfinance OHLC + `adj_close` + `volume`; sembol `.IS` soneksiz) |

Hafta sonu ve resmî tatillerde satır oluşmaz; boşluklar staging katmanında ele alınır.

Notlar (BIST):
- `close` ham kapanış, `adj_close` temettü/bedelsiz sermaye artırımı düzeltmeli kapanıştır; getiri ve
  göstergeler `adj_close` üzerinden hesaplanır. Yahoo geçmiş `adj_close` değerlerini sonradan güncelleyebilir;
  upsert bu düzeltmeleri tekrar çekimde üzerine yazar.
- Seans kapanmadan yapılan çekimde günün satırı ara değerdir (XU100 hacmi 0 görünebilir). Günlük DAG seans
  sonrasına zamanlanır; aynı gün tekrar çalışırsa upsert satırı kesin değerle günceller.

## Neden bu seçim?

Anahtar gerektirmeyen tek bir kaynakla (yfinance) proje herkes tarafından `git clone` + `docker compose up`
ile çalıştırılabilir. EVDS, resmî kur karşılaştırması için opsiyonel ikinci kaynak olarak eklenir.
