{#-
  Birden çok sütunun birlikte tekil olduğunu doğrular (doğal anahtar). dbt_utils'teki eşdeğerinin
  bağımlılıksız sürümü: ağsız ortamda `dbt deps` gerektirmeden çalışsın diye projede tutulur.
-#}
{% test unique_combination_of_columns(model, combination_of_columns) %}
select
    {{ combination_of_columns | join(', ') }},
    count(*) as n_rows
from {{ model }}
group by {{ combination_of_columns | join(', ') }}
having count(*) > 1
{% endtest %}
