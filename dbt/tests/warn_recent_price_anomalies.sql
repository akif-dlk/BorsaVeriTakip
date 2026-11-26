{#-
  Son günlerdeki error düzeyli fiyat anormallikleri (uyarı). Geçmişteki gerçek şoklar (ör. Aralık 2021 USDTRY)
  günlük build'i sonsuza dek kırmasın diye yalnızca son 10 takvim günü sayılır ve önem derecesi warn'dur:
  DAG düşmez, kayıt test raporuna ve ops.dbt_test_results'a uyarı olarak düşer (alarm: Gün 13).
-#}
{{ config(severity='warn') }}
{%- set lookback_days = 10 %}
select instrument_code, price_date, anomaly_type, metric_value, threshold, adj_close, prev_adj_close
from {{ ref('mart_price_anomalies') }}
where severity = 'error'
  and price_date >= (now() at time zone 'Europe/Istanbul')::date - {{ lookback_days }}
