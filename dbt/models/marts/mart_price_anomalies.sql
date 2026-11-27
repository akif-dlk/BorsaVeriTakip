-- Anormal fiyat tespiti. Tanecik: enstrüman × işlem günü × kural; yalnızca anormal satırlar (boş tablo = temiz veri).
-- Kurallar ve eşikler Python başvuru uygulamasıyla aynıdır (src/bvt/anomalies.py; testler ikisini karşılaştırır):
--   return_spike (error): |basit getiri| > varlık sınıfı eşiği
--   zscore_spike (warn) : log getirinin önceki 60 getiriye (bugün hariç, en az 20) göre |z| > 5
--   stale_price  (warn) : aynı adj_close art arda ≥ 5 işlem günü (5. günden itibaren her gün)
--   zero_volume  (warn) : hissede hacim = 0
--   invalid_ohlc (warn) : has_valid_ohlc = false
-- Satırlar fact_daily_prices'tan elenmez; bu tablo gözden geçirme ve alarm (Gün 13) içindir. Son günlerdeki
-- error kayıtları singular test (tests/warn_recent_price_anomalies.sql) ile test raporuna uyarı olarak düşer.
{%- set return_thresholds = {'fx': 0.1, 'gold': 0.1, 'index': 0.15, 'stock': 0.25} %}
{%- set zscore_window = 60 %}
{%- set zscore_min_obs = 20 %}
{%- set zscore_threshold = 5 %}
{%- set stale_run_days = 5 %}
{%- set min_std = 1e-12 %}
with prices as (

    select
        instrument_key,
        date_key,
        instrument_code,
        price_date,
        asset_class,
        adj_close,
        volume,
        has_valid_ohlc,
        lag(adj_close::double precision) over (partition by instrument_code order by price_date) as prev_adj_close
    from {{ ref('fact_daily_prices') }}

),

returns as (

    select
        *,
        case when adj_close > 0 and prev_adj_close > 0 then adj_close::double precision / prev_adj_close - 1 end
            as simple_return,
        case when adj_close > 0 and prev_adj_close > 0 then ln(adj_close::double precision / prev_adj_close) end
            as log_return,
        case when prev_adj_close is null or adj_close::double precision <> prev_adj_close then 1 else 0 end
            as is_new_level,
        case asset_class
            {%- for asset_class, threshold in return_thresholds.items() %}
            when '{{ asset_class }}' then {{ threshold }}
            {%- endfor %}
        end as return_threshold
    from prices

),

stats as (

    select
        *,
        avg(log_return) over history          as hist_mean,
        stddev_samp(log_return) over history  as hist_std,
        count(log_return) over history        as hist_n,
        sum(is_new_level) over (partition by instrument_code order by price_date rows unbounded preceding) as level_id
    from returns
    window history as (
        partition by instrument_code order by price_date
        rows between {{ zscore_window }} preceding and 1 preceding
    )

),

scored as (

    select
        *,
        case when hist_n >= {{ zscore_min_obs }} and hist_std > {{ min_std }}
            then (log_return - hist_mean) / hist_std end                                   as return_zscore,
        row_number() over (partition by instrument_code, level_id order by price_date)     as stale_run_length
    from stats

),

anomalies as (

    select *, 'return_spike' as anomaly_type, 'error' as severity,
           simple_return as metric_value, return_threshold::double precision as threshold
    from scored
    where abs(simple_return) > return_threshold

    union all

    select *, 'zscore_spike', 'warn', return_zscore, {{ zscore_threshold }}::double precision
    from scored
    where abs(return_zscore) > {{ zscore_threshold }}

    union all

    select *, 'stale_price', 'warn', stale_run_length::double precision, {{ stale_run_days }}::double precision
    from scored
    where stale_run_length >= {{ stale_run_days }}

    union all

    select *, 'zero_volume', 'warn', 0::double precision, null::double precision
    from scored
    where asset_class = 'stock' and volume = 0

    union all

    select *, 'invalid_ohlc', 'warn', null::double precision, null::double precision
    from scored
    where has_valid_ohlc = false

)

select
    instrument_key,
    date_key,
    instrument_code,
    price_date,
    asset_class,
    anomaly_type,
    severity,
    metric_value,
    threshold,
    adj_close,
    prev_adj_close
from anomalies
