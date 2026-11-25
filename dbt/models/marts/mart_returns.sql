-- Günlük getiriler. Tanecik: enstrüman × işlem günü (fact_daily_prices ile birebir).
-- Tanımlar Python başvuru uygulamasıyla aynıdır (src/bvt/returns.py daily_returns; testler ikisini karşılaştırır):
--   simple_return = adj_close / önceki işlem günü - 1, log_return = ln(adj_close / önceki) (mart_indicators ile aynı),
--   cumulative_return = adj_close / enstrümanın ilk adj_close'u - 1. Fiyat ≤ 0 ise getiri NULL.
-- Her enstrüman kendi işlem günleri üzerindedir; önceki gün tatil/eksikse getiri aradaki günleri de kapsar
-- (days_since_prev takvim günü farkıdır: hafta sonu sonrası 3, bayram sonrası daha fazla).
-- mart_period_returns (haftalık/aylık özet) ve mart_correlations bu modelden türetilir.
with prices as (

    select
        instrument_key,
        date_key,
        instrument_code,
        price_date,
        asset_class,
        adj_close,
        volume,
        row_number() over w                             as obs_index,
        lag(price_date) over w                          as prev_price_date,
        lag(adj_close::double precision) over w         as prev_adj_close,
        first_value(adj_close::double precision) over w as first_adj_close
    from {{ ref('fact_daily_prices') }}
    window w as (partition by instrument_code order by price_date)

)

select
    instrument_key,
    date_key,
    instrument_code,
    price_date,
    asset_class,
    obs_index,
    adj_close,
    volume,
    prev_price_date,
    price_date - prev_price_date as days_since_prev,
    case when adj_close > 0 and prev_adj_close > 0 then adj_close::double precision / prev_adj_close - 1 end as simple_return,
    case when adj_close > 0 and prev_adj_close > 0 then ln(adj_close::double precision / prev_adj_close) end as log_return,
    case when first_adj_close > 0 then adj_close::double precision / first_adj_close - 1 end as cumulative_return
from prices
