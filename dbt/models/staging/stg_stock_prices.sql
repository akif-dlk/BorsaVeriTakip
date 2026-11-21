-- BIST hisseleri ve XU100: ortak fiyat sözleşmesine çevrilir.
-- Getiri/göstergeler için adj_close kullanılır; kaynak vermediyse ham kapanışa düşülür.
-- Endekslerde hacim anlamlı değildir (Yahoo 0 döndürür) → NULL.
with source as (

    select * from {{ source('raw', 'stock_prices') }}

),

renamed as (

    select
        upper(trim(symbol))                                     as instrument_code,
        case when upper(trim(symbol)) like 'XU%' then 'index'
             else 'stock' end                                   as asset_class,
        price_date::date                                        as price_date,
        lower(trim(source))                                     as source,
        'TRY'::text                                             as currency,
        open::numeric(18, 6)                                    as open,
        high::numeric(18, 6)                                    as high,
        low::numeric(18, 6)                                     as low,
        close::numeric(18, 6)                                   as close,
        coalesce(adj_close, close)::numeric(18, 6)              as adj_close,
        case when upper(trim(symbol)) like 'XU%' then nullif(volume, 0)
             else volume end                                    as volume,
        true                                                    as is_primary_source,
        inserted_at,
        fetched_at                                              as loaded_at
    from source

)

select
    *,
    {{ ohlc_is_valid('open', 'high', 'low', 'close') }} as has_valid_ohlc
from renamed
