# BorsaVeriTakip

Döviz (USD/TRY, EUR/TRY), BIST hisseleri ve altın için uçtan uca veri mühendisliği projesi:
Python + PostgreSQL + Airflow + dbt + Kafka + FastAPI + Streamlit, hepsi Docker ile.

> 🚧 Geliştirme sürüyor: proje 20 günlük bir plana göre adım adım inşa ediliyor. İlerleme için [ROADMAP](docs/roadmap.md).

## Hızlı başlangıç

```bash
cp .env.example .env
docker compose up -d postgres
pip install -r requirements.txt
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

## Belgeler

- [Mimari](docs/architecture.md)
- [Veri kaynakları ve kullanım koşulları](docs/data_sources.md)
- [Yol haritası](docs/roadmap.md)

## Repo yapısı

```
├── docker-compose.yml   # servisler (şimdilik PostgreSQL)
├── sql/init/            # ilk açılışta çalışan şema betikleri
├── src/bvt/             # Python paketi: config, db, raw_loader, çekiciler
├── docs/                # mimari, kaynaklar, yol haritası
└── tests/
```

---
Veriler yalnızca eğitim amaçlıdır, yatırım tavsiyesi değildir.
