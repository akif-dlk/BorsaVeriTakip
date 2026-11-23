-- Takvim boyutu: ilk fiyat gününden bugüne (İstanbul) kesintisiz gün dizisi; anahtar YYYYMMDD tamsayısı.
-- Tatil takvimi tutulmaz, veriden çıkarılır (bvt.gaps ile aynı yaklaşım): hafta içi olup hiçbir BIST
-- enstrümanında verisi olmayan ve son BIST gününden eski olan gün `is_bist_holiday`dir (bugünün henüz gelmemiş
-- verisi tatil sayılmaz). Gösterge ve getiri hesaplarında (Gün 10-11) seriler bu takvime hizalanır.
with prices as (

    select price_date, 'fx' as feed from {{ ref('stg_fx_rates') }} where is_primary_source
    union all
    select price_date, 'bist' as feed from {{ ref('stg_stock_prices') }} where is_primary_source
    union all
    select price_date, 'gold' as feed from {{ ref('stg_gold_prices') }} where instrument_code = 'XAUUSD_OZ'

),

coverage as (

    select
        price_date,
        bool_or(feed = 'fx')    as has_fx_data,
        bool_or(feed = 'bist')  as has_bist_data,
        bool_or(feed = 'gold')  as has_gold_data
    from prices
    group by price_date

),

bounds as (

    -- Tek satır (toplama); veri yoksa first_day NULL olur ve takvim boş kalır.
    select
        min(price_date)                                                          as first_day,
        greatest(max(price_date), (now() at time zone 'Europe/Istanbul')::date)  as last_day,
        max(price_date) filter (where feed = 'bist')                             as last_bist_day
    from prices

),

spine as (

    select generate_series(b.first_day, b.last_day, interval '1 day')::date as date_day
    from bounds as b
    where b.first_day is not null

),

calendar as (

    select
        s.date_day,
        extract(isodow from s.date_day)::int        as iso_day_of_week,   -- 1 = Pazartesi
        extract(isodow from s.date_day) in (6, 7)   as is_weekend,
        coalesce(c.has_fx_data, false)              as has_fx_data,
        coalesce(c.has_bist_data, false)            as has_bist_data,
        coalesce(c.has_gold_data, false)            as has_gold_data
    from spine as s
    left join coverage as c on c.price_date = s.date_day

)

select
    to_char(cal.date_day, 'YYYYMMDD')::int                                      as date_key,
    cal.date_day,
    extract(year from cal.date_day)::int                                        as year,
    extract(quarter from cal.date_day)::int                                     as quarter,
    extract(month from cal.date_day)::int                                       as month,
    (array['Ocak', 'Şubat', 'Mart', 'Nisan', 'Mayıs', 'Haziran', 'Temmuz', 'Ağustos', 'Eylül', 'Ekim',
           'Kasım', 'Aralık'])[extract(month from cal.date_day)::int]           as month_name_tr,
    extract(day from cal.date_day)::int                                         as day_of_month,
    cal.iso_day_of_week,
    (array['Pazartesi', 'Salı', 'Çarşamba', 'Perşembe', 'Cuma', 'Cumartesi',
           'Pazar'])[cal.iso_day_of_week]                                       as day_name_tr,
    extract(isoyear from cal.date_day)::int                                     as iso_year,
    extract(week from cal.date_day)::int                                        as iso_week,
    date_trunc('week', cal.date_day)::date                                      as week_start_date,
    date_trunc('month', cal.date_day)::date                                     as month_start_date,
    cal.is_weekend,
    cal.has_fx_data,
    cal.has_bist_data,
    cal.has_gold_data,
    cal.has_bist_data                                                           as is_bist_trading_day,
    coalesce(not cal.is_weekend and not cal.has_bist_data
             and cal.date_day <= b.last_bist_day, false)                        as is_bist_holiday
from calendar as cal
cross join bounds as b
