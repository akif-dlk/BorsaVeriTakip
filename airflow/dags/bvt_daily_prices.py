"""
### bvt_daily_prices

Döviz (USD/TRY, EUR/TRY), BIST (8 hisse + XU100) ve altını (ons + gram) her iş günü raw katmana yükler.

- **Zamanlama:** Pzt-Cum 19:30 (Europe/Istanbul). BIST 18:00'de kapanır, yfinance ~15 dk gecikmelidir.
- **Pencere:** `data_interval_end` gününün son `BVT_LOOKBACK_DAYS` (varsayılan 7) günü. Yükleme idempotent
  olduğundan tekrar çekim kopya üretmez; kaçırılan günler ve kaynak revizyonları kendiliğinden düzelir.
- **Görevler:** `fetch_fx`, `fetch_stocks`, `fetch_gold` birbirinden bağımsızdır ve paralel çalışır; biri
  başarısız olursa diğerleri yine yüklenir. Her görev `ops.pipeline_runs`'a `run_trigger='airflow'` ile kayıt düşer.
  Ardından `check_gaps` (çekimler başarısız olsa da çalışır) son 30 günün boşluk raporunu `ops.data_gaps`'e yazar.
  Paralelinde `dbt_build_staging` raw kaynak testlerini ve `staging.stg_*` görünümlerini (dbt) kurar/test eder;
  başarılı olursa `dbt_build_marts` star schema'yı (`marts.dim_instrument`, `dim_date`, `fact_daily_prices`) ve
  türetilmiş tabloları (`marts.mart_indicators`, `mart_returns`, `mart_period_returns`, `mart_correlations`) kurar.
- **Retry:** iki katman. Çekici içinde HTTP isteği üstel bekleme ile 4 kez denenir (hız sınırında en az 30 sn);
  o da tükenirse Airflow görevi 5, 10 dk... (üst sınır 30 dk) arayla 2 kez daha dener.
- İş mantığı `bvt.pipeline` / `bvt.fetch_*` modüllerindedir; bu dosya yalnızca orkestrasyonu tanımlar.
"""
from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.trigger_rule import TriggerRule

from bvt.pipeline import SOURCES  # yalnızca standart kütüphane; DAG ayrıştırmasını yavaşlatmaz

DAG_ID = "bvt_daily_prices"
SCHEDULE = "30 19 * * 1-5"


def fetch_source(source: str, data_interval_end=None, **_context) -> dict:
    """Airflow bağlamından pencere sonunu alıp ilgili çekiciyi çalıştırır; özet XCom'a döner."""
    from bvt.pipeline import run_daily  # ağır bağımlılıklar yalnızca görev çalışırken yüklenir

    return run_daily(source, data_interval_end, trigger="airflow")


def check_gaps(data_interval_end=None, **_context) -> dict:
    """Boşluk raporu: tatilleri gerçek eksiklerden ayırır, ops.data_gaps'e yazar; özet XCom'a döner."""
    from bvt.pipeline import check_gaps as run_check

    return run_check(data_interval_end)


def dbt_build_staging(**_context) -> dict:
    """raw kaynak testleri + staging görünümleri (dbt build); test başarısızsa görev düşer. Özet XCom'a döner."""
    from bvt.dbt_runner import build_staging

    return build_staging()


def dbt_build_marts(**_context) -> dict:
    """Star schema (seed + dim_* + fact_*) tabloları ve testleri (dbt build). Özet XCom'a döner."""
    from bvt.dbt_runner import build_marts

    return build_marts()


default_args = {
    "owner": "bvt",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(minutes=30),
}

with DAG(
    dag_id=DAG_ID,
    description="Döviz, BIST ve altın günlük fiyatlarını raw katmana yükler",
    schedule=SCHEDULE,
    start_date=pendulum.datetime(2026, 11, 2, tz="Europe/Istanbul"),
    catchup=False,  # geçmiş yükleme CLI ile parça parça yapılır (bkz. README)
    max_active_runs=1,
    default_args=default_args,
    tags=["bvt", "raw", "dbt", "daily"],
    doc_md=__doc__,
) as dag:
    fetch_tasks = [
        PythonOperator(
            task_id=f"fetch_{name}",
            python_callable=fetch_source,
            op_kwargs={"source": name},
        )
        for name in SOURCES
    ]
    gap_check = PythonOperator(
        task_id="check_gaps",
        python_callable=check_gaps,
        trigger_rule=TriggerRule.ALL_DONE,  # bir kaynak düşse de diğerlerinin boşlukları raporlanır
        retries=0,
    )
    staging = PythonOperator(
        task_id="dbt_build_staging",
        python_callable=dbt_build_staging,
        # Görünümler raw'a bağlıdır, çekimin başarısına değil: bir kaynak düşse de katman kurulup test edilir.
        trigger_rule=TriggerRule.ALL_DONE,
        retries=0,  # dbt hataları (test/derleme) deterministiktir; yeniden denemek düzeltmez
    )
    marts = PythonOperator(
        task_id="dbt_build_marts",
        python_callable=dbt_build_marts,
        # Varsayılan all_success: staging testi düşerse hatalı veri marts tablolarına taşınmaz.
        retries=0,
    )
    fetch_tasks >> gap_check
    fetch_tasks >> staging >> marts
