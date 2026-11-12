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
```

Çekiciler tek istekte toplu indirme yapar, hata/boş yanıtta üstel bekleme ile yeniden dener ve her çalışmayı
`ops.pipeline_runs` tablosuna kaydeder. `EVDS_API_KEY` doluysa TCMB gösterge kuru ikinci kaynak olarak eklenir.

## Belgeler

- [Mimari](docs/architecture.md)
- [Veri kaynakları ve kullanım koşulları](docs/data_sources.md)
- [Yol haritası](docs/roadmap.md)

## Repo yapısı

```
├── docker-compose.yml   # servisler (şimdilik PostgreSQL)
├── sql/init/            # ilk açılışta çalışan şema betikleri
├── src/bvt/             # Python paketi: config, db, çekiciler
├── docs/                # mimari, kaynaklar, yol haritası
└── tests/
```

---
Veriler yalnızca eğitim amaçlıdır, yatırım tavsiyesi değildir.
