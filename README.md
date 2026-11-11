# BorsaVeriTakip

Döviz (USD/TRY, EUR/TRY), BIST hisseleri ve altın için uçtan uca veri mühendisliği projesi:
Python + PostgreSQL + Airflow + dbt + Kafka + FastAPI + Streamlit, hepsi Docker ile.

> 🚧 Geliştirme sürüyor: proje 20 günlük bir plana göre adım adım inşa ediliyor. İlerleme için [ROADMAP](docs/roadmap.md).

## Hızlı başlangıç

```bash
cp .env.example .env
docker compose up -d postgres
pip install -r requirements.txt
PYTHONPATH=src python -m bvt.db   # PostgreSQL sürümünü yazdırırsa bağlantı tamam
```

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
