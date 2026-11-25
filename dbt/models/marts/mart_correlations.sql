-- Enstrüman çiftleri arasında hareketli getiri korelasyonu (altın-dolar-BIST ve diğer tüm çiftler).
-- Tanecik: çift (instrument_a < instrument_b, bayt sırası; her çift bir kez) × ortak işlem günü.
-- Tanımlar Python başvuru uygulamasıyla aynıdır (src/bvt/returns.py rolling_correlations):
--   iki enstrümanın da günlük log getirisi olan ortak günlerde, son n ortak gözlemin Pearson korelasyonu
--   (n = 60 ≈ çeyrek, 252 ≈ yıl); n gözlem dolmadan NULL, serilerden biri pencerede sabitse NULL.
--   Kayan nokta taşmalarına karşı [-1, 1] aralığına kırpılır.
-- Bilinen sınırlama: takvimler farklıdır (BIST tatilleri, ABD vadeli altın); tatil sonrası getiri birden fazla
-- günü kapsayabilir. Korelasyon yalnızca ortak günlerde ölçülür, eksik gün ileri doldurulmaz.
-- Isı haritası için son gün: `where price_date = (select max(price_date) from marts.mart_correlations)` veya
-- bvt.returns.correlation_matrix(); tablo yalnızca a < b yarısını tutar, simetriği ayna ile elde edilir.
{%- set windows = [60, 252] %}
with returns as (

    select instrument_key, date_key, instrument_code, asset_class, price_date, log_return
    from {{ ref('mart_returns') }}
    where log_return is not null

),

pairs as (

    select
        a.instrument_key   as instrument_a_key,
        b.instrument_key   as instrument_b_key,
        a.instrument_code  as instrument_a,
        b.instrument_code  as instrument_b,
        a.asset_class      as asset_class_a,
        b.asset_class      as asset_class_b,
        a.date_key,
        a.price_date,
        a.log_return       as return_a,
        b.log_return       as return_b,
        row_number() over (
            partition by a.instrument_code, b.instrument_code order by a.price_date
        ) as pair_obs_index
    from returns as a
    join returns as b
        on b.price_date = a.price_date
        and a.instrument_code < b.instrument_code collate "C"

),

rolling as (

    select
        p.*
        {%- for n in windows %},
        corr(p.return_a, p.return_b) over (
            partition by p.instrument_a, p.instrument_b order by p.pair_obs_index
            rows between {{ n - 1 }} preceding and current row
        ) as corr_{{ n }}d_raw
        {%- endfor %}
    from pairs as p

)

select
    instrument_a_key,
    instrument_b_key,
    instrument_a,
    instrument_b,
    asset_class_a,
    asset_class_b,
    date_key,
    price_date,
    pair_obs_index,
    {%- for n in windows %}
    case when pair_obs_index >= {{ n }} then greatest(-1.0, least(1.0, corr_{{ n }}d_raw)) end as corr_{{ n }}d{% if not loop.last %},{% endif %}
    {%- endfor %}
from rolling
