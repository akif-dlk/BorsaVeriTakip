# Mimari (taslak)

```mermaid
flowchart LR
    subgraph Kaynaklar
        YF[yfinance<br/>BIST, FX, GC=F]
        EVDS[TCMB EVDS<br/>opsiyonel]
    end

    subgraph Batch["Batch (günlük)"]
        AF[Airflow DAG<br/>bvt_daily_prices] --> F1[fetch_fx.py]
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
| `raw` | Kaynaktan geldiği gibi; doğal anahtar `(enstrüman, tarih, kaynak)` üzerinde idempotent upsert (`bvt.raw_loader`) | Python çekiciler |
| `staging` | Temizlenmiş, tipleri düzeltilmiş `stg_*` görünümleri | dbt |
| `marts` | Star schema + göstergeler, korelasyonlar | dbt |
| `streaming` | Dakikalık fiyatlar ve pencere ortalamaları | Kafka consumer |
| `ops` | `pipeline_runs` (çalışma kayıtları + tetikleyici), `data_gaps` (tatil / eksik gün raporu), `dbt_test_results` (dbt test sonuçları) | Python çekiciler, Airflow |

## Geçmiş yükleme ve boşluklar

- `bvt.backfill` aralığı 365 günlük parçalara böler, parçalar arasında bekler, her parçayı ayrı çalışma
  (`run_trigger='backfill'`) olarak kaydeder. `ops.pipeline_runs`'ta başarılı bir pencerenin kapsadığı parçalar
  atlanır (devam ettirilebilir). İlk hatada durur (kaynak büyük olasılıkla hız sınırında).
- `bvt.gaps` tatil takvimi tutmaz, veriden çıkarır: iş gününde kaynağın hiçbir enstrümanında veri yoksa
  `market_closed`, bir kısmında yoksa `missing`. Enstrümanın ilk görüldüğü tarihten öncesi sayılmaz.
  `--repair-gaps` yalnızca `missing` günleri (yakın günleri birleştirerek) yeniden çeker.
- Fiyat serilerindeki tatil boşluklarının takvime hizalanması / ileri doldurulması dbt katmanında (`dim_date`).

## Dönüşüm (dbt)

- **Proje:** `dbt/` (profil `bvt`, bağlantı `POSTGRES_*` ortam değişkenlerinden). `generate_schema_name`
  makrosu özel şemayı olduğu gibi kullanır: staging modelleri `staging`, şeması belirtilmeyenler `marts`
  şemasına yazılır (dbt'nin varsayılan `marts_staging` önekini kullanmaz).
- **Kaynaklar:** `raw.fx_rates`, `raw.stock_prices`, `raw.gold_prices` (`_sources.yml`); tazelik `fetched_at`
  üzerinden (uyarı 4 gün, hata 10 gün; hafta sonu ve bayram tatilleri sığsın diye).
- **Staging (görünüm):** `stg_fx_rates`, `stg_stock_prices`, `stg_gold_prices` aynı sütunları aynı anlamla
  taşır (ortak fiyat sözleşmesi); `fact_daily_prices` (Gün 9) bunları birleştirir. Dönüşümler: kod büyük harf,
  tip/ölçek sabitleme, `asset_class` (fx / stock / index / gold), `adj_close` (yoksa `close`), endeks hacminde
  0 → NULL, `is_primary_source` (döviz için yfinance; EVDS karşılaştırma kaynağı), `has_valid_ohlc` bayrağı.
- **Marts / star schema (tablo):** `fact_daily_prices` (tanecik: enstrüman × işlem günü, yalnızca birincil
  kaynak, tek satır) → `dim_instrument` ve `dim_date`. Vekil anahtarlar kararlıdır: `instrument_key =
  md5(instrument_code)` (`surrogate_key` makrosu), `date_key = YYYYMMDD`; tablolar her build'de baştan kurulsa da
  bağlar değişmez. `dim_instrument` = `instrument_metadata` seed'i (ad, sektör, kaynak sembolü; `staging`
  şemasına yüklenir) + gözlenen kapsam (ilk/son gün, gün sayısı); seed'de olmayan yeni sembol `is_configured=false`
  ile görünür (uyarı testi). `dim_date` ilk fiyat gününden bugüne kesintisiz takvimdir; tatiller veriden çıkarılır
  (`is_bist_holiday`: hafta içi, son BIST gününden eski ve BIST verisi yok), `has_fx/bist/gold_data` bayrakları
  serilerin takvime hizalanmasında (Gün 10-11) kullanılır.
- **Göstergeler (`mart_indicators`, tablo):** tanecik `fact_daily_prices` ile aynı. SMA(20/50/200) pencere
  fonksiyonuyla; EMA(12/26, ilk kapanışla tohumlanır) ve Wilder RSI(14) önceki değere bağlı olduğundan özyineli
  CTE ile (fiyatlar enstrüman başına diziye alınır, her adım ~13 satır işler); volatilite = son 20 log getirinin
  örneklem std'si × √252. Seriler enstrümanın kendi işlem günleridir (tatil/eksik gün atlanır, ileri doldurma
  yok); pencere dolmadan NULL. Aynı tanımlar `bvt.indicators`'ta (pandas) başvuru uygulaması olarak tutulur;
  PostgreSQL testi ikisini karşılaştırır, birim testi SQL'deki pencere ayarlarının Python sabitleriyle aynı
  olduğunu denetler.
- **Getiriler ve korelasyonlar (tablo):** `mart_returns` (tanecik `fact_daily_prices` ile aynı; basit/log/birikimli
  getiri, `days_since_prev`) → `mart_period_returns` (enstrüman × dönem tipi × dönem başı; ISO hafta ve takvim
  ayı, getiri bir önceki verisi olan dönemin son kapanışına göre, dönem içi volatilite, `is_complete` = dönemin
  takvim sonu bugünden önce) ve `mart_correlations` (çift × ortak gün; `instrument_a < instrument_b collate "C"`,
  her çift bir kez; iki tarafın da getirisi olan ortak günlerde 60/252 gözlemlik `corr()` pencere fonksiyonu,
  pencere dolmadan NULL, [-1, 1]'e kırpılır). Takvimler farklı olduğundan (BIST tatili, ABD vadeli altın) tatil
  sonrası getiri birden fazla günü kapsayabilir; ileri doldurma yapılmaz. Başvuru uygulaması `bvt.returns`
  (`correlation_matrix()` ısı haritası/API için simetrik matris üretir).
- **Anormal fiyat tespiti (`mart_price_anomalies`, tablo):** enstrüman × gün × kural, yalnızca anormal satırlar.
  `return_spike` (|basit getiri| > varlık eşiği: fx/altın %10, endeks %15, hisse %25; error), `zscore_spike`
  (önceki 60 log getiriye göre |z| > 5, en az 20 gözlem; warn), `stale_price` (aynı fiyat ≥ 5 işlem günü; warn),
  `zero_volume` (hisse; warn), `invalid_ohlc` (warn). Olgudan satır elenmez; tablo gözden geçirme ve alarm içindir.
  Başvuru uygulaması `bvt.anomalies`.
- **Testler:** not_null, unique, accepted_values, relationships; bağımlılıksız generic testler
  `unique_combination_of_columns` (doğal anahtar), `accepted_range` (ör. RSI 0-100, fiyat > 0 `inclusive: false`;
  NULL'lar hariç) ve `not_in_future` (tarih İstanbul takvimine göre ileride olamaz). Singular testler
  (`dbt/tests/*.sql`): `assert_gram_gold_matches_ounce` (gram = ons × USDTRY / 31.1034768, binde 1 tolerans; error),
  `assert_fact_covers_primary_prices` (olgu, birincil kaynağın tüm günlerini taşır; error),
  `warn_recent_price_anomalies` (son 10 günde error düzeyli anormallik; warn — geçmiş gerçek şoklar build'i
  kırmaz). Marts testleri staging'e ref vermez (eager seçimde staging build'inde eski tablolara karşı çalışmasın
  diye). `dbt deps` gerekmez; ağsız ortamda da çalışır.
- **Test raporu:** `bvt.dbt_runner` her build'den sonra (başarısız olsa da) `run_results.json` + `manifest.json`'ı
  `bvt.dbt_report` ile `ops.dbt_test_results`'a yazar (çağrı × test; tür, model, sütun, önem, durum, bozan satır
  sayısı). `dbt_test_report` görevi son 6 saatin en güncel sonuçlarını ve 30 günlük anormallik özetini
  `test_report.md` / `test_report.json` olarak yazar (dbt hedef dizini veya `BVT_REPORT_DIR`).
- **Çalıştırma:** dbt ayrı sanal ortamda (`/opt/dbt-venv`, `requirements-dbt.txt`); `bvt.dbt_runner` alt süreç
  olarak çağırır, çıktıyı satır satır loglar, hata/süre aşımında görevi düşürür. Proje konteynere salt-okunur
  bağlanır; `target/` ve loglar `DBT_TARGET_PATH` / `DBT_LOG_PATH` ile `/tmp/dbt` altına yazılır.

## Orkestrasyon (Airflow)

- **Servisler:** `airflow-webserver` (:8080), `airflow-scheduler`, tek seferlik `airflow-init`; LocalExecutor.
  Airflow metadata'sı ayrı bir PostgreSQL'de (`airflow-db`) tutulur, proje verisi (`postgres`) ile karışmaz.
- **Kod:** `src/` ve `sql/` konteynere salt-okunur bağlanır (`PYTHONPATH=/opt/bvt/src`); imaj yalnızca
  bağımlılıkları içerir, kod değişikliği için yeniden derleme gerekmez.
- **DAG `bvt_daily_prices`:** Pzt-Cum 19:30 İstanbul, `catchup=False`, `max_active_runs=1`. Üç bağımsız
  `PythonOperator` görevi `bvt.pipeline.run_daily(kaynak, data_interval_end)` çağırır. Pencere: son
  `BVT_LOOKBACK_DAYS` gün (idempotent yeniden çekim). Görev özeti (kaynak, pencere, satır sayısı) XCom'a,
  ayrıntılı sayımlar `ops.pipeline_runs`'a yazılır.
- **dbt:** `dbt_build_staging` görevi üç çekimden sonra (`trigger_rule=all_done`) `dbt build --select
  source:raw staging` çalıştırır; test başarısızsa görev düşer, yeniden denenmez (`retries=0`). Ardından
  `dbt_build_marts` (`all_success`: staging testi düşerse hatalı veri marts'a taşınmaz) `dbt build --select
  instrument_metadata marts` ile seed'i ve star schema tablolarını kurup test eder. Her iki görev test
  sonuçlarını `ops.dbt_test_results`'a yazar; `dbt_test_report` (`all_done`: dbt düşse de) günlük test raporunu
  üretir, özetini XCom'a döner.
- **Boşluk kontrolü:** `check_gaps` görevi üç çekimden sonra (`trigger_rule=all_done`, biri düşse de) çalışır,
  son `BVT_GAP_CHECK_DAYS` günü `bvt.gaps` ile sınıflandırıp `ops.data_gaps`'e yazar; görevi düşürmez.
- **Retry:** istek düzeyinde `bvt.retry` (üstel bekleme, yalnızca geçici hatalar, hız sınırında ≥30 sn), görev
  düzeyinde Airflow (`retries=2`, `retry_exponential_backoff`, `max_retry_delay=30 dk`).
- DAG dosyası projeden üst düzeyde yalnızca `bvt.pipeline`ı içe aktarır (o da yalnızca standart kütüphane kullanır; scheduler ayrıştırması hızlı kalır);
  pandas/yfinance görev çalışırken yüklenir.
