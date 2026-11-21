{#-
  OHLC tutarlılığı: low ≤ open/close ≤ high. Eksik alan (ör. EVDS'te yalnızca close) tutarsızlık sayılmaz.
  Satır elenmez, bayraklanır; anormal fiyat tespiti (Gün 12) bu bayrağı kullanır.
-#}
{% macro ohlc_is_valid(open, high, low, close) -%}
    coalesce(
        {{ low }} <= {{ high }}
        and {{ close }} between {{ low }} and {{ high }}
        and ({{ open }} is null or {{ open }} between {{ low }} and {{ high }}),
        true
    )
{%- endmacro %}
