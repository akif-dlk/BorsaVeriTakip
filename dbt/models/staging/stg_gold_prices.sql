-- Altın: ons (USD, yfinance GC=F) ve gram (TRY, ons × USDTRY / 31.1034768) ortak fiyat sözleşmesine çevrilir.
-- usdtry yalnızca gram satırlarında doludur: dönüşümde kullanılan kur (izlenebilirlik).
with source as (

    select * from {{ source('raw', 'gold_prices') }}

),

renamed as (

    select
        upper(trim(instrument))                as instrument_code,
        'gold'::text                           as asset_class,
        price_date::date                       as price_date,
        lower(trim(source))                    as source,
        upper(trim(currency))                  as currency,
        open::numeric(18, 6)                   as open,
        high::numeric(18, 6)                   as high,
        low::numeric(18, 6)                    as low,
        close::numeric(18, 6)                  as close,
        close::numeric(18, 6)                  as adj_close,
        null::bigint                           as volume,
        true                                   as is_primary_source,  -- her enstrümanın tek kaynağı var
        usdtry::numeric(18, 6)                 as usdtry,
        inserted_at,
        fetched_at                             as loaded_at
    from source

)

select
    *,
    {{ ohlc_is_valid('open', 'high', 'low', 'close') }} as has_valid_ohlc
from renamed
