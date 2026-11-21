-- Döviz kurları: ortak fiyat sözleşmesine (instrument_code, price_date, source, OHLC, ...) çevrilir.
-- yfinance birincil kaynaktır; EVDS (opsiyonel) yalnızca gösterge kur verir, karşılaştırma için tutulur.
with source as (

    select * from {{ source('raw', 'fx_rates') }}

),

renamed as (

    select
        upper(trim(pair))                      as instrument_code,
        'fx'::text                             as asset_class,
        rate_date::date                        as price_date,
        lower(trim(source))                    as source,
        right(upper(trim(pair)), 3)            as currency,      -- kotasyon para birimi (USDTRY → TRY)
        open::numeric(18, 6)                   as open,
        high::numeric(18, 6)                   as high,
        low::numeric(18, 6)                    as low,
        close::numeric(18, 6)                  as close,
        close::numeric(18, 6)                  as adj_close,     -- kurlarda düzeltme yok
        null::bigint                           as volume,
        lower(trim(source)) = 'yfinance'       as is_primary_source,
        inserted_at,
        fetched_at                             as loaded_at
    from source

)

select
    *,
    {{ ohlc_is_valid('open', 'high', 'low', 'close') }} as has_valid_ohlc
from renamed
