-- Teknik göstergeler. Tanecik: enstrüman × işlem günü (fact_daily_prices ile birebir).
-- Tanımlar Python başvuru uygulamasıyla aynıdır (src/bvt/indicators.py; testler ikisini karşılaştırır):
--   SMA(20/50/200), EMA(12/26; ilk kapanışla tohumlanır, α = 2/(n+1)), RSI(14; Wilder, α = 1/14),
--   20 günlük yıllıklandırılmış volatilite (log getirilerin örneklem std'si × √252).
-- Her enstrüman kendi işlem günleri üzerinde hesaplanır: tatil/eksik günler atlanır, ileri doldurma yapılmaz.
-- Pencere dolmadan (ısınma dönemi) gösterge NULL'dır; obs_index ile hangi günden itibaren dolu olduğu görülür.
-- EMA ve RSI önceki değere bağlı (özyineli) olduğundan pencere fonksiyonuyla değil özyineli CTE ile hesaplanır.
-- Fiyatlar enstrüman başına diziye alınır; özyineleme her adımda yalnızca ~13 satır işler (dizi erişimi O(1)).
{%- set sma_windows = [20, 50, 200] %}
{%- set ema_spans = [12, 26] %}
{%- set rsi_window = 14 %}
{%- set vol_window = 20 %}
{%- set trading_days = 252 %}
with recursive

prices as (

    select
        instrument_key,
        date_key,
        instrument_code,
        price_date,
        asset_class,
        adj_close,
        row_number() over (partition by instrument_code order by price_date) as obs_index
    from {{ ref('fact_daily_prices') }}

),

series as (

    select
        instrument_code,
        array_agg(adj_close::double precision order by obs_index) as closes,
        count(*)                                                  as n_obs
    from prices
    group by instrument_code

),

-- Özyineli düzleştirme: EMA'lar ve RSI'ın kazanç/kayıp ortalamaları. İlk gün EMA = kapanış, RSI ortalaması yok;
-- ikinci gün ortalamalar ilk fiyat farkıyla tohumlanır.
smoothed (instrument_code, obs_index{% for n in ema_spans %}, ema_{{ n }}{% endfor %}, avg_gain, avg_loss) as (

    select
        instrument_code,
        1::bigint,
        {%- for n in ema_spans %}
        closes[1],
        {%- endfor %}
        null::double precision,
        null::double precision
    from series

    union all

    select
        s.instrument_code,
        r.obs_index + 1,
        {%- for n in ema_spans %}
        (2.0 / ({{ n }} + 1)) * s.closes[r.obs_index + 1] + (1 - 2.0 / ({{ n }} + 1)) * r.ema_{{ n }},
        {%- endfor %}
        case when r.obs_index = 1 then greatest(s.closes[2] - s.closes[1], 0)
             else (1.0 / {{ rsi_window }}) * greatest(s.closes[r.obs_index + 1] - s.closes[r.obs_index], 0)
                  + (1 - 1.0 / {{ rsi_window }}) * r.avg_gain end,
        case when r.obs_index = 1 then greatest(s.closes[1] - s.closes[2], 0)
             else (1.0 / {{ rsi_window }}) * greatest(s.closes[r.obs_index] - s.closes[r.obs_index + 1], 0)
                  + (1 - 1.0 / {{ rsi_window }}) * r.avg_loss end
    from smoothed as r
    join series as s on s.instrument_code = r.instrument_code
    where r.obs_index < s.n_obs

),

daily_returns as (

    select
        p.*,
        case when p.adj_close > 0 and lag(p.adj_close) over w > 0
             then ln(p.adj_close::double precision / lag(p.adj_close::double precision) over w) end as log_return
    from prices as p
    window w as (partition by p.instrument_code order by p.obs_index)

),

windowed as (

    select
        r.*,
        {%- for n in sma_windows %}
        avg(r.adj_close::double precision) over (
            partition by r.instrument_code order by r.obs_index rows between {{ n - 1 }} preceding and current row
        ) as sma_{{ n }}_raw,
        {%- endfor %}
        stddev_samp(r.log_return) over (
            partition by r.instrument_code order by r.obs_index
            rows between {{ vol_window - 1 }} preceding and current row
        ) as volatility_raw,
        count(r.log_return) over (
            partition by r.instrument_code order by r.obs_index
            rows between {{ vol_window - 1 }} preceding and current row
        ) as n_returns_in_window
    from daily_returns as r

),

indicators as (

    select
        w.instrument_key,
        w.date_key,
        w.instrument_code,
        w.price_date,
        w.asset_class,
        w.obs_index,
        w.adj_close,
        w.log_return,
        {%- for n in sma_windows %}
        case when w.obs_index >= {{ n }} then w.sma_{{ n }}_raw end as sma_{{ n }},
        {%- endfor %}
        {%- for n in ema_spans %}
        case when w.obs_index >= {{ n }} then sm.ema_{{ n }} end as ema_{{ n }},
        {%- endfor %}
        case
            when w.obs_index <= {{ rsi_window }} then null
            when sm.avg_loss = 0 and sm.avg_gain = 0 then 50.0
            when sm.avg_loss = 0 then 100.0
            else 100.0 - 100.0 / (1 + sm.avg_gain / sm.avg_loss)
        end as rsi_{{ rsi_window }},
        case when w.n_returns_in_window = {{ vol_window }}
             then w.volatility_raw * sqrt({{ trading_days }}) end as volatility_{{ vol_window }}d
    from windowed as w
    join smoothed as sm
        on sm.instrument_code = w.instrument_code
        and sm.obs_index = w.obs_index

)

select
    instrument_key,
    date_key,
    instrument_code,
    price_date,
    asset_class,
    obs_index,
    adj_close,
    log_return,
    sma_20,
    sma_50,
    sma_200,
    ema_12,
    ema_26,
    rsi_14,
    case when rsi_14 >= 70 then 'overbought'
         when rsi_14 <= 30 then 'oversold'
         when rsi_14 is not null then 'neutral' end as rsi_zone,
    volatility_20d
from indicators
