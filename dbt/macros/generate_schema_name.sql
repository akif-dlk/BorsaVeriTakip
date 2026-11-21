{#-
  dbt varsayılanı özel şemayı hedef şemanın önüne ekler (marts_staging). Bu projede katmanlar
  sql/init/001_schemas.sql'de sabit şemalardır (staging, marts); özel şema adı olduğu gibi kullanılır.
-#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
