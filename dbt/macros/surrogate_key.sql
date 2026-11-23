{#-
  Doğal anahtardan kararlı vekil anahtar (md5 metni). dbt_utils.generate_surrogate_key'in bağımlılıksız sürümü:
  aynı girdi her build'de aynı anahtarı verir; tablo yeniden kurulsa da olgu-boyut bağları değişmez.
  NULL bileşenler sabit bir yer tutucuya çevrilir, bileşenler '|' ile ayrılır (('ab','c') ile ('a','bc') çakışmaz).
-#}
{% macro surrogate_key(columns) -%}
    md5(
        {%- for col in columns %}
        coalesce(cast({{ col }} as text), '_bvt_null_')
        {%- if not loop.last %} || '|' || {% endif -%}
        {%- endfor %}
    )
{%- endmacro %}
