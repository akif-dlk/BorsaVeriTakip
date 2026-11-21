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

## dbt: staging katmanı

dbt projesi `dbt/` altındadır. `raw.*` tabloları kaynak (`source('raw', ...)`) olarak tanımlıdır; üç staging
görünümü (`staging.stg_fx_rates`, `stg_stock_prices`, `stg_gold_prices`) bunları **ortak bir fiyat sözleşmesine**
çevirir: `instrument_code, asset_class, price_date, source, currency, open, high, low, close, adj_close, volume,
is_primary_source, has_valid_ohlc, loaded_at`. Satırlar elenmez; OHLC tutarsızlığı `has_valid_ohlc` ile
bayraklanır. Kaynak ve modeller için not-null, kabul edilen değer ve doğal anahtar tekilliği testleri vardır;
raw tablolarda `fetched_at` üzerinden tazelik (freshness) eşiği tanımlıdır.

dbt, Airflow ile bağımlılık çakışması olmasın diye **ayrı bir sanal ortama** kurulur (imajda `/opt/dbt-venv`).
`bvt.dbt_runner` dbt'yi `.env` yüklenmiş olarak ve proje/profil dizinini vererek çağırır. Günlük DAG'de
`dbt_build_staging` görevi üç çekimden sonra çalışır (`dbt build --select source:raw staging`).

```bash
python -m venv .venv-dbt && .venv-dbt/bin/pip install -r requirements-dbt.txt   # Windows: .venv-dbt\Scripts\pip
export DBT_BIN=.venv-dbt/bin/dbt                                                 # PATH'te dbt varsa gerekmez
PYTHONPATH=src python -m bvt.dbt_runner debug                                    # bağlantı kontrolü
PYTHONPATH=src python -m bvt.dbt_runner build --select source:raw staging        # görünümler + testler
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
├── dbt/                 # dbt projesi: models/staging (stg_*), makrolar, generic testler, profiles.yml
├── sql/init/            # ilk açılışta çalışan şema betikleri
├── src/bvt/             # Python paketi: config, db, raw_loader, çekiciler, pipeline, backfill, gaps, retry, dbt_runner
├── docs/                # mimari, kaynaklar, yol haritası
└── tests/
```

---
Veriler yalnızca eğitim amaçlıdır, yatırım tavsiyesi değildir.
