-- Enstrüman boyutu: tanım (seed) + gözlenen veri kapsamı. Seed'de olup henüz verisi olmayan enstrüman da
-- listelenir; staging'de görülüp seed'de tanımı olmayan yeni bir sembol de kaybolmaz (adı NULL, is_configured=false).
with metadata as (

    select * from {{ ref('instrument_metadata') }}

),

prices as (

    select instrument_code, asset_class, currency, price_date
    from {{ ref('stg_fx_rates') }} where is_primary_source
    union all
    select instrument_code, asset_class, currency, price_date
    from {{ ref('stg_stock_prices') }} where is_primary_source
    union all
    select instrument_code, asset_class, currency, price_date
    from {{ ref('stg_gold_prices') }} where is_primary_source

),

observed as (

    select
        instrument_code,
        min(asset_class)    as asset_class,
        min(currency)       as currency,
        min(price_date)     as first_price_date,
        max(price_date)     as last_price_date,
        count(*)            as n_price_days
    from prices
    group by instrument_code

),

joined as (

    select
        coalesce(m.instrument_code, o.instrument_code)   as instrument_code,
        m.instrument_name,
        coalesce(m.asset_class, o.asset_class)           as asset_class,
        coalesce(m.currency, o.currency)                 as currency,
        nullif(trim(m.sector), '')                       as sector,
        m.source_ticker,
        o.first_price_date,
        o.last_price_date,
        coalesce(o.n_price_days, 0)                      as n_price_days,
        m.instrument_code is not null                    as is_configured
    from metadata as m
    full outer join observed as o
        on o.instrument_code = m.instrument_code

)

select
    {{ surrogate_key(['instrument_code']) }} as instrument_key,
    *
from joined
