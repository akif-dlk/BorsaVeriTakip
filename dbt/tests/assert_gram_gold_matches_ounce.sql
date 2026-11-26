{#-
  Gram altın tutarlılığı (error): her XAUTRY_GRAM satırı aynı günün ons fiyatı × kayıtlı USDTRY / 31.1034768 ile
  binde 1 toleransla eşleşmeli ve aynı gün ons satırı bulunmalı. Bozulursa fetch_gold'daki türetme ya da
  ons/kur hizalaması hatalıdır; gram fiyattan türeyen tüm göstergeler etkilenir.
  Başarısız satırlar: price_date, gram_close, expected_close, rel_diff, problem.
-#}
{%- set troy_ounce_grams = 31.1034768 %}
{%- set tolerance = 0.001 %}
with gram as (

    select price_date, close, usdtry
    from {{ ref('stg_gold_prices') }}
    where instrument_code = 'XAUTRY_GRAM'

),

ounce as (

    select price_date, close
    from {{ ref('stg_gold_prices') }}
    where instrument_code = 'XAUUSD_OZ' and is_primary_source

),

compared as (

    select
        g.price_date,
        g.close                                                   as gram_close,
        o.close * g.usdtry / {{ troy_ounce_grams }}               as expected_close,
        case
            when o.close is null then 'ounce_missing'
            when g.usdtry is null or g.usdtry <= 0 then 'usdtry_missing'
        end                                                       as problem
    from gram as g
    left join ounce as o on o.price_date = g.price_date

)

select
    price_date,
    gram_close,
    expected_close,
    abs(gram_close - expected_close) / nullif(gram_close, 0)      as rel_diff,
    coalesce(problem, 'mismatch')                                 as problem
from compared
where problem is not null
   or abs(gram_close - expected_close) > {{ tolerance }} * gram_close
