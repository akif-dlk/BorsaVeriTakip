{#-
  Sütun değerinin [min_value, max_value] aralığında olduğunu doğrular; NULL değerler test dışıdır
  (ör. gösterge ısınma dönemi). Sınırlardan biri verilmezse o yönde kontrol yapılmaz.
  dbt_utils.accepted_range'in bağımlılıksız sürümü (`dbt deps` gerektirmez).
-#}
{% test accepted_range(model, column_name, min_value=none, max_value=none) %}
select {{ column_name }}
from {{ model }}
where {{ column_name }} is not null
  and (
      false
      {%- if min_value is not none %} or {{ column_name }} < {{ min_value }}{% endif %}
      {%- if max_value is not none %} or {{ column_name }} > {{ max_value }}{% endif %}
  )
{% endtest %}
