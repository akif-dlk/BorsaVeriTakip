{#-
  Olgu kapsamı (error): fact_daily_prices her enstrümanın birincil kaynaktaki tüm günlerini taşımalı
  (dim_instrument.n_price_days). Eksik satır, dim_date / dim_instrument join'inde düşen kayıt demektir
  (ör. takvim dışında kalan tarih). Fazla satır ise aynı gün için çift birincil kaynak.
-#}
with fact_counts as (

    select instrument_key, count(*) as n_fact_days
    from {{ ref('fact_daily_prices') }}
    group by instrument_key

)

select
    i.instrument_code,
    i.n_price_days,
    coalesce(f.n_fact_days, 0) as n_fact_days
from {{ ref('dim_instrument') }} as i
left join fact_counts as f on f.instrument_key = i.instrument_key
where i.n_price_days <> coalesce(f.n_fact_days, 0)
