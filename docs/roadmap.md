# Yol haritası

## Hafta 1: Temel ve veri çekimi
- [x] Gün 1: Repo yapısı, mimari taslağı, docker-compose ile PostgreSQL, kaynak seçimi
- [ ] Gün 2: Döviz çekici (USD/TRY, EUR/TRY), hata yönetimi → `fetch_fx.py`
- [ ] Gün 3: BIST hisse çekici (8 hisse + BIST100), geçmiş veri → `fetch_stocks.py`
- [ ] Gün 4: Altın çekici, gram altın hesabı → `fetch_gold.py`
- [ ] Gün 5: Raw şema, idempotent upsert
- [ ] Gün 6: Airflow kurulumu, günlük DAG
- [ ] Gün 7: Retry, loglama, backfill, tatil boşlukları

## Hafta 2: Dönüşüm ve modelleme
- [ ] Gün 8: dbt kurulumu, `stg_*` modelleri
- [ ] Gün 9: `dim_instrument`, `dim_date`, `fact_daily_prices`
- [ ] Gün 10: SMA, EMA, RSI, volatilite → `mart_indicators`
- [ ] Gün 11: Getiriler, haftalık/aylık özet → `mart_correlations`
- [ ] Gün 12: dbt testleri, anormal fiyat tespiti
- [ ] Gün 13: Telegram/e-posta alarmı
- [ ] Gün 14: Refactor, birim testleri

## Hafta 3: Streaming, API, UI
- [ ] Gün 15: Redpanda + producer
- [ ] Gün 16: Consumer → `intraday_prices`, pencere ortalaması
- [ ] Gün 17: FastAPI `/prices`, `/indicators`, `/correlations`
- [ ] Gün 18: Streamlit grafikleri
- [ ] Gün 19: Gösterge katmanları, ısı haritası, canlı panel, sağlık durumu
- [ ] Gün 20: GitHub Actions CI, README, diyagram, demo
