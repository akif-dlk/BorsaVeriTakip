-- Günlük fiyat olgusu. Tanecik: enstrüman × işlem günü, tek satır (birincil kaynak).
-- Üç staging modeli ortak fiyat sözleşmesini taşıdığından doğrudan birleştirilir. Aynı enstrüman-gün için birden
-- fazla birincil satır gelirse (gelmemeli) en son yükleneni alınır; tanecik testi yine de korur.
-- Satırlar elenmez: OHLC tutarsızlığı has_valid_ohlc ile taşınır (anormal fiyat tespiti, Gün 12).
-- Tablo her build'de baştan kurulur (veri küçük: ~13 enstrüman × iş günü); kaynak revizyonları böylece yansır.
with prices as (

    select instrument_code, price_date, source, open, high, low, close, adj_close, volume, has_valid_ohlc, loaded_at
    from {{ ref('stg_fx_rates') }} where is_primary_source
    union all
    select instrument_code, price_date, source, open, high, low, close, adj_close, volume, has_valid_ohlc, loaded_at
    from {{ ref('stg_stock_prices') }} where is_primary_source
    union all
    select instrument_code, price_date, source, open, high, low, close, adj_close, volume, has_valid_ohlc, loaded_at
    from {{ ref('stg_gold_prices') }} where is_primary_source

),

deduplicated as (

    select
        *,
        row_number() over (partition by instrument_code, price_date order by loaded_at desc, source) as rn
    from prices

)

select
    i.instrument_key,
    d.date_key,
    p.instrument_code,
    p.price_date,
    i.asset_class,
    i.currency,
    p.source,
    p.open,
    p.high,
    p.low,
    p.close,
    p.adj_close,
    p.volume,
    p.has_valid_ohlc,
    p.loaded_at
from deduplicated as p
join {{ ref('dim_instrument') }} as i on i.instrument_code = p.instrument_code
join {{ ref('dim_date') }} as d on d.date_day = p.price_date
where p.rn = 1
