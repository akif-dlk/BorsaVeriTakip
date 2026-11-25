-- Haftalık ve aylık özet. Tanecik: enstrüman × dönem tipi × dönem başı (yalnızca verisi olan dönemler).
-- Tanımlar Python başvuru uygulamasıyla aynıdır (src/bvt/returns.py period_summary):
--   hafta ISO'dur (pazartesi başlar; dim_date.week_start_date ile aynı), ay takvim ayıdır;
--   period_return = dönemin son kapanışı / bir önceki (verisi olan) dönemin son kapanışı - 1 (ilk dönemde NULL);
--   volatility = dönem içi günlük log getirilerin örneklem std'si × √252 (en az 2 getiri);
--   is_complete = dönemin takvim sonu bugünden (İstanbul) önce; sürmekte olan dönemin değerleri değişebilir.
-- Kapanışlar adj_close'tur (düzeltilmiş); hacim yalnızca hisselerde dolu (diğerlerinde NULL).
{%- set periods = ['week', 'month'] %}
{%- set trading_days = 252 %}
with daily as (

    select
        r.instrument_key,
        r.instrument_code,
        r.asset_class,
        r.price_date,
        r.adj_close::double precision as adj_close,
        r.volume,
        r.log_return,
        p.period_type,
        date_trunc(p.period_type, r.price_date::timestamp)::date as period_start_date
    from {{ ref('mart_returns') }} as r
    cross join (
        values {% for p in periods %}('{{ p }}'){% if not loop.last %}, {% endif %}{% endfor %}
    ) as p (period_type)

),

periods as (

    select
        instrument_key,
        instrument_code,
        asset_class,
        period_type,
        period_start_date,
        min(price_date)                                       as first_trading_date,
        max(price_date)                                       as last_trading_date,
        count(*)                                              as n_trading_days,
        (array_agg(adj_close order by price_date))[1]         as first_close,
        (array_agg(adj_close order by price_date desc))[1]    as last_close,
        max(adj_close)                                        as max_close,
        min(adj_close)                                        as min_close,
        stddev_samp(log_return)                               as return_std,
        count(log_return)                                     as n_returns,
        sum(volume)                                           as total_volume
    from daily
    group by instrument_key, instrument_code, asset_class, period_type, period_start_date

),

with_previous as (

    select
        p.*,
        (p.period_start_date + ('1 ' || p.period_type)::interval)::date - 1 as period_end_date,
        lag(p.last_close) over (
            partition by p.instrument_code, p.period_type order by p.period_start_date
        ) as prev_period_close
    from periods as p

)

select
    instrument_key,
    instrument_code,
    asset_class,
    period_type,
    period_start_date,
    period_end_date,
    first_trading_date,
    last_trading_date,
    n_trading_days,
    first_close,
    last_close,
    max_close,
    min_close,
    prev_period_close,
    case when last_close > 0 and prev_period_close > 0 then last_close / prev_period_close - 1 end as period_return,
    case when last_close > 0 and prev_period_close > 0 then ln(last_close / prev_period_close) end as period_log_return,
    case when n_returns >= 2 then return_std * sqrt({{ trading_days }}) end as volatility,
    total_volume,
    period_end_date < (now() at time zone 'Europe/Istanbul')::date as is_complete
from with_previous
