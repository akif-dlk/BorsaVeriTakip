# BorsaVeriTakip

Döviz (USD/TRY, EUR/TRY), BIST hisseleri ve altın için uçtan uca veri mühendisliği projesi:
Python + PostgreSQL + Airflow + dbt + Kafka + FastAPI + Streamlit, hepsi Docker ile.

> 🚧 Geliştirme sürüyor: proje 20 günlük bir plana göre adım adım inşa ediliyor. İlerleme için [ROADMAP](docs/roadmap.md).

## Hızlı başlangıç

```bash
cp .env.example .env
docker compose up -d postgres
pip install -r requirements-dev.txt   # çalışma zamanı + test bağımlılıkları
PYTHONPATH=src python -m bvt.db --init   # bağlantıyı test eder, sql/init şemalarını uygular
```

## Veri çekiciler

```bash
# Döviz (USD/TRY, EUR/TRY) → raw.fx_rates
PYTHONPATH=src python -m bvt.fetch_fx                      # son 30 gün
PYTHONPATH=src python -m bvt.fetch_fx --start 2020-01-01   # geçmiş yükleme
PYTHONPATH=src python -m bvt.fetch_fx --days 7 --dry-run   # DB'ye yazmadan önizleme

# BIST (8 hisse + XU100) → raw.stock_prices
PYTHONPATH=src python -m bvt.fetch_stocks                          # son 30 gün
PYTHONPATH=src python -m bvt.fetch_stocks --start 2018-01-01       # geçmiş yükleme (yıllık parçalar halinde)
PYTHONPATH=src python -m bvt.fetch_stocks --symbols THYAO XU100 --days 7 --dry-run

# Altın: ons (GC=F, USD) + gram altın (TRY = ons × USDTRY / 31.1034768) → raw.gold_prices
PYTHONPATH=src python -m bvt.fetch_gold                       # son 30 gün
PYTHONPATH=src python -m bvt.fetch_gold --start 2015-01-01    # geçmiş yükleme (yıllık parçalar halinde)
PYTHONPATH=src python -m bvt.fetch_gold --days 7 --dry-run
```

> Mevcut bir veritabanına yeni ham tablolar eklendiğinde `python -m bvt.db --init` tekrar çalıştırılır
> (betikler `IF NOT EXISTS` ile yazıldı, güvenlidir).

## Raw katman ve idempotent yükleme

Üç çekici de ortak `bvt.raw_loader` sözleşmesiyle yükler: satırlar `COPY` ile geçici tabloya yazılır, tek bir
`INSERT ... ON CONFLICT DO UPDATE` ile doğal anahtar `(enstrüman, tarih, kaynak)` üzerinden aktarılır.
Aynı veri ikinci kez gelirse kopya oluşmaz ve satır yeniden yazılmaz; yalnızca değeri değişen (ör. Yahoo
`adj_close` revizyonu) satırlar güncellenir. Her çalışma `ops.pipeline_runs`'a yeni/güncellenen/değişmeyen
satır sayılarıyla kaydedilir.

```bash
PYTHONPATH=src python -m bvt.db --summary   # raw tablolarda enstrüman başına satır sayısı ve tarih aralığı
BVT_TEST_PG=1 python -m pytest -q tests/test_raw_loader_pg.py   # gerçek PostgreSQL'e karşı idempotentlik testi
```

Çekiciler tek istekte toplu indirme yapar, hata/boş yanıtta üstel bekleme ile yeniden dener ve her çalışmayı
`ops.pipeline_runs` tablosuna kaydeder. Uzun geçmiş yüklemeleri parçalara bölünür; her parça
ayrı istekle çekilip hemen yüklenir ve parçalar arasında kısa bekleme yapılır. `EVDS_API_KEY` doluysa TCMB gösterge kuru ikinci kaynak olarak eklenir.

## Airflow: günlük orkestrasyon

`docker compose up -d` PostgreSQL'i ve Airflow'u (LocalExecutor, ayrı metadata veritabanı) ayağa kaldırır.
`airflow-init` tek seferlik çalışır: Airflow metadata şemasını taşır, yönetici kullanıcıyı oluşturur ve proje
şemasını (`sql/init`) uygular.

```bash
cp .env.example .env
docker compose up -d --build        # ilk açılış birkaç dakika sürer (imaj derlenir)
# Arayüz: http://localhost:8080  (kullanıcı/şifre: .env'deki AIRFLOW_ADMIN_USER / AIRFLOW_ADMIN_PASSWORD)
docker compose exec airflow-scheduler airflow dags list-import-errors
docker compose exec airflow-scheduler airflow dags trigger bvt_daily_prices   # elle tetikleme
```

`bvt_daily_prices` DAG'i Pzt-Cum 19:30'da (İstanbul) çalışır; `fetch_fx`, `fetch_stocks`, `fetch_gold`
görevleri paralel ve birbirinden bağımsızdır, ardından `check_gaps` boşluk raporunu yazar. Her görev çalışmanın `data_interval_end` gününden geriye
`BVT_LOOKBACK_DAYS` (varsayılan 7) günü yeniden çeker; yükleme idempotent olduğundan bu kopya üretmez, kaçırılan
günleri ve kaynak revizyonlarını yakalar. DAG yalnızca orkestrasyonu tanımlar; iş mantığı `bvt.pipeline` ve
`bvt.fetch_*` modüllerindedir. Aynı günlük pencere Airflow olmadan da çalıştırılabilir:

```bash
PYTHONPATH=src python -m bvt.pipeline                              # üç kaynak, bugünle biten pencere
PYTHONPATH=src python -m bvt.pipeline --source fx gold --lookback-days 3
```

## Retry, loglama, backfill ve tatil boşlukları

- **Retry (iki katman):** çekiciler HTTP isteklerini üstel bekleme ile 4 kez dener (2, 4, 8 sn + jitter); hız
  sınırı (HTTP 429 / `YFRateLimitError`) gelirse en az 30 sn beklenir. Yalnızca geçici hatalar (ağ, zaman aşımı,
  hız sınırı, boş yanıt) yeniden denenir; programlama hataları hemen düşer. Veritabanı bağlantısı da geçici
  hatalarda yeniden denenir. Bunlar da tükenirse Airflow görevi üstel aralıkla (5 dk → en fazla 30 dk) 2 kez
  daha dener.
- **Loglama:** tüm CLI'lar ortak `bvt.logging_setup` kullanır. `BVT_LOG_LEVEL`, `BVT_LOG_FORMAT=json` (tek satır
  JSON: zaman, seviye, kaynak, pencere, satır sayısı, süre, tetikleyici). Her çalışma `ops.pipeline_runs`'a
  tetikleyicisiyle (`manual` / `airflow` / `backfill` / `repair`) yazılır.
- **Backfill:** parça parça, rate limit dostu ve **devam ettirilebilir**: başarıyla yüklenmiş parçalar atlanır,
  yarıda kalan iş aynı komutla sürer.
- **Tatil boşlukları:** hafta sonu yok sayılır; bir iş gününde kaynağın hiçbir enstrümanında veri yoksa
  `market_closed` (tatil), diğerlerinde var ama bir enstrümanda yoksa `missing` (gerçek eksik) olarak
  `ops.data_gaps`'e yazılır. DAG her çalışmadan sonra son 30 günü kontrol eder (`check_gaps` görevi).

```bash
PYTHONPATH=src python -m bvt.backfill --start 2018-01-01                    # üç kaynak, kaldığı yerden devam eder
PYTHONPATH=src python -m bvt.backfill --source stocks --start 2015-01-01 --pause 5 --dry-run   # yalnızca plan
PYTHONPATH=src python -m bvt.gaps --days 90 --show-closed                   # boşluk raporu
PYTHONPATH=src python -m bvt.backfill --repair-gaps --days 90               # gerçek eksikleri yeniden çek
BVT_LOG_FORMAT=json PYTHONPATH=src python -m bvt.pipeline --source fx       # JSON log
```

## dbt: staging ve star schema

dbt projesi `dbt/` altındadır. `raw.*` tabloları kaynak (`source('raw', ...)`) olarak tanımlıdır; üç staging
görünümü (`staging.stg_fx_rates`, `stg_stock_prices`, `stg_gold_prices`) bunları **ortak bir fiyat sözleşmesine**
çevirir: `instrument_code, asset_class, price_date, source, currency, open, high, low, close, adj_close, volume,
is_primary_source, has_valid_ohlc, loaded_at`. Satırlar elenmez; OHLC tutarsızlığı `has_valid_ohlc` ile
bayraklanır. Kaynak ve modeller için not-null, kabul edilen değer ve doğal anahtar tekilliği testleri vardır;
raw tablolarda `fetched_at` üzerinden tazelik (freshness) eşiği tanımlıdır.

Üzerinde `marts` şemasında bir **star schema** kuruludur: `fact_daily_prices` (enstrüman × işlem günü, birincil
kaynak) → `dim_instrument` (seed `dbt/seeds/instrument_metadata.csv` + veri kapsamı) ve `dim_date` (kesintisiz
takvim, veriden çıkarılan BIST tatilleri). Anahtarlar kararlıdır (`md5(instrument_code)`, `YYYYMMDD`); olgu
tanecik tekilliği ve boyutlara referans bütünlüğü (relationships) test edilir.

`marts.mart_indicators` aynı taneciğe (enstrüman × işlem günü) teknik göstergeleri ekler: SMA(20/50/200),
EMA(12/26), Wilder RSI(14) ve bölgesi (`overbought`/`oversold`/`neutral`), 20 günlük yıllıklandırılmış volatilite
ve günlük log getiri. Her enstrüman kendi işlem günleri üzerinde `adj_close` ile hesaplanır; pencere dolmadan
gösterge NULL'dır. Tanımların Python karşılığı `src/bvt/indicators.py`'dedir; `tests/test_mart_indicators_pg.py`
(`BVT_TEST_PG=1`) SQL ile Python sonuçlarını aynı veri üzerinde karşılaştırır.

Getiri ve korelasyon katmanı (`src/bvt/returns.py` başvuru uygulaması, `tests/test_mart_correlations_pg.py`):

- `marts.mart_returns` — enstrüman × işlem günü: basit, log ve birikimli getiri; `days_since_prev` (tatil sonrası
  getirinin kaç takvim gününü kapsadığı).
- `marts.mart_period_returns` — haftalık (ISO, pazartesi) ve aylık özet: ilk/son/en yüksek/en düşük kapanış,
  önceki döneme göre getiri, dönem içi yıllıklandırılmış volatilite, toplam hacim, `is_complete`.
- `marts.mart_correlations` — her enstrüman çifti (a < b, tek yön) için ortak işlem günlerinde günlük log
  getirilerin 60 ve 252 günlük hareketli Pearson korelasyonu (altın-dolar-BIST dahil).

```sql
-- Son gün altın / dolar / BIST100 korelasyonları
select instrument_a, instrument_b, corr_60d, corr_252d
from marts.mart_correlations
where price_date = (select max(price_date) from marts.mart_correlations)
  and instrument_a in ('USDTRY', 'XAUTRY_GRAM', 'XU100') and instrument_b in ('USDTRY', 'XAUTRY_GRAM', 'XU100');

-- Aylık getiriler
select instrument_code, period_start_date, period_return, volatility, is_complete
from marts.mart_period_returns
where period_type = 'month' and instrument_code = 'XU100'
order by period_start_date desc limit 12;
```

```sql
select price_date, adj_close, sma_50, sma_200, rsi_14, rsi_zone, volatility_20d
from marts.mart_indicators
where instrument_code = 'THYAO' and sma_200 is not null
order by price_date desc limit 10;
```

```sql
select d.year, d.month, i.instrument_name, avg(f.adj_close) as ortalama
from marts.fact_daily_prices f
join marts.dim_instrument i using (instrument_key)
join marts.dim_date d using (date_key)
where i.asset_class = 'stock'
group by 1, 2, 3 order by 1, 2, 3;
```

### Veri kalitesi: dbt testleri, anormal fiyatlar, test raporu

Her katmanda testler vardır: not_null / unique / accepted_values / relationships, doğal anahtar tekilliği,
`accepted_range` (fiyatlar > 0, hacim ≥ 0, RSI 0-100, korelasyon [-1, 1]) ve `not_in_future` (İstanbul takvimine
göre ileri tarihli fiyat yok). Singular testler (`dbt/tests/`): gram altının ons × USDTRY / 31.1034768 ile tutarlı
olması, olgunun birincil kaynağın tüm günlerini taşıması ve son 10 gündeki ciddi fiyat anormallikleri (uyarı).

`marts.mart_price_anomalies` anormal fiyatları satır elemeden işaretler (başvuru uygulaması `src/bvt/anomalies.py`):

| Kural | Koşul | Önem |
|---|---|---|
| `return_spike` | \|günlük getiri\| > fx/altın %10, endeks %15, hisse %25 (düzeltilmemiş bölünme, kaynak hatası) | error |
| `zscore_spike` | önceki 60 log getiriye göre \|z\| > 5 | warn |
| `stale_price` | aynı kapanış ≥ 5 işlem günü | warn |
| `zero_volume` | hissede hacim 0 | warn |
| `invalid_ohlc` | low ≤ open/close ≤ high bozuk | warn |

Her `dbt build` (DAG'de staging ve marts) sonrasında test sonuçları `ops.dbt_test_results`'a yazılır;
`dbt_test_report` görevi son 6 saatin sonuçları ve 30 günlük anormallik özetiyle `test_report.md` /
`test_report.json` üretir (dbt hedef dizini; konteynerde `/tmp/dbt/target`, `BVT_REPORT_DIR` ile değiştirilebilir).
Mevcut bir veritabanına yeni tabloyu eklemek için `PYTHONPATH=src python -m bvt.db --init`.

```bash
PYTHONPATH=src python -m bvt.dbt_report                  # veritabanından günlük test raporu
PYTHONPATH=src python -m bvt.dbt_report --from-target    # veritabanı olmadan, son run_results.json'dan
PYTHONPATH=src python -m bvt.dbt_report --record --fail-on-error   # kaydet + başarısız test varsa çıkış 1
BVT_TEST_PG=1 python -m pytest -q tests/test_mart_anomalies_pg.py  # SQL ↔ Python anormallik karşılaştırması
```

```sql
select price_date, instrument_code, anomaly_type, severity, metric_value, threshold
from marts.mart_price_anomalies
where price_date >= current_date - 30
order by severity, price_date desc;
```

dbt, Airflow ile bağımlılık çakışması olmasın diye **ayrı bir sanal ortama** kurulur (imajda `/opt/dbt-venv`).
`bvt.dbt_runner` dbt'yi `.env` yüklenmiş olarak ve proje/profil dizinini vererek çağırır. Günlük DAG'de
`dbt_build_staging` görevi üç çekimden sonra çalışır (`dbt build --select source:raw staging`), başarılı
olursa `dbt_build_marts` star schema'yı kurar (`dbt build --select instrument_metadata marts`).

```bash
python -m venv .venv-dbt && .venv-dbt/bin/pip install -r requirements-dbt.txt   # Windows: .venv-dbt\Scripts\pip
export DBT_BIN=.venv-dbt/bin/dbt                                                 # PATH'te dbt varsa gerekmez
PYTHONPATH=src python -m bvt.dbt_runner debug                                    # bağlantı kontrolü
PYTHONPATH=src python -m bvt.dbt_runner build --select source:raw staging        # görünümler + testler
PYTHONPATH=src python -m bvt.dbt_runner build --select instrument_metadata marts  # seed + dim_*/fact_*/mart_*
PYTHONPATH=src python -m bvt.dbt_runner source freshness                         # raw tazelik raporu
docker compose exec airflow-scheduler python -m bvt.dbt_runner build             # konteyner içinde
```

## Belgeler

- [Mimari](docs/architecture.md)
- [Veri kaynakları ve kullanım koşulları](docs/data_sources.md)
- [Yol haritası](docs/roadmap.md)

## Repo yapısı

```
├── docker-compose.yml   # servisler: PostgreSQL, Airflow (webserver, scheduler, init, metadata DB)
├── airflow/             # Dockerfile (Airflow + proje bağımlılıkları + ayrı dbt ortamı), dags/
├── dbt/                 # dbt projesi: models/staging (stg_*), models/marts (dim_*, fact_*, mart_*), seeds, makrolar, generic + singular testler
├── sql/init/            # ilk açılışta çalışan şema betikleri
├── src/bvt/             # Python paketi: config, db, raw_loader, çekiciler, pipeline, backfill, gaps, retry, dbt_runner, dbt_report, indicators, returns, anomalies
├── docs/                # mimari, kaynaklar, yol haritası
└── tests/
```

---
Veriler yalnızca eğitim amaçlıdır, yatırım tavsiyesi değildir.
