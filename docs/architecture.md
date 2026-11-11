# Mimari (taslak)

```mermaid
flowchart LR
    subgraph Kaynaklar
        YF[yfinance<br/>BIST, FX, GC=F]
        EVDS[TCMB EVDS<br/>opsiyonel]
    end

    subgraph Batch["Batch (günlük)"]
        AF[Airflow DAG] --> F1[fetch_fx.py]
        AF --> F2[fetch_stocks.py]
        AF --> F3[fetch_gold.py]
    end

    subgraph Stream["Streaming (dakikalık)"]
        P[Producer] --> K[(Kafka / Redpanda)] --> C[Consumer]
    end

    YF --> F1 & F2 & F3
    EVDS --> F1
    YF --> P

    subgraph PG["PostgreSQL"]
        RAW[(raw.*)] --> DBT[dbt] --> STG[(staging.stg_*)] --> MART[(marts.dim_*, fact_*, mart_*)]
        INTRA[(streaming.intraday_prices)]
    end

    F1 & F2 & F3 -->|upsert| RAW
    C --> INTRA

    MART --> API[FastAPI]
    INTRA --> API
    API --> UI[Streamlit]
    AF -.->|hata / bayat veri| TG[Telegram / e-posta]
```

## Katmanlar

| Şema | İçerik | Sahibi |
|---|---|---|
| `raw` | Kaynaktan geldiği gibi, `(sembol, tarih)` üzerinde upsert | Python çekiciler |
| `staging` | Temizlenmiş, tipleri düzeltilmiş `stg_*` görünümleri | dbt |
| `marts` | Star schema + göstergeler, korelasyonlar | dbt |
| `streaming` | Dakikalık fiyatlar ve pencere ortalamaları | Kafka consumer |
| `ops` | Pipeline çalışma kayıtları, sağlık durumu | Airflow |
