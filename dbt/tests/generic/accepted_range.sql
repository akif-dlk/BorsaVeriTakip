{#-
  Sütun değerinin [min_value, max_value] aralığında olduğunu doğrular; NULL değerler test dışıdır
  (ör. gösterge ısınma dönemi). Sınırlardan biri verilmezse o yönde kontrol yapılmaz.
  inclusive=false sınırları dışlar (ör. fiyat > 0: min_value=0, inclusive=false).
  dbt_utils.accepted_range'in bağımlılıksız sürümü (`dbt deps` gerektirmez).
-#}
{% test accepted_range(model, column_name, min_value=none, max_value=none, inclusive=true) %}
select {{ column_name }}
from {{ model }}
where {{ column_name }} is not null
  and (
      false
      {%- if min_value is not none %} or {{ column_name }} {{ '<' if inclusive else '<=' }} {{ min_value }}{% endif %}
      {%- if max_value is not none %} or {{ column_name }} {{ '>' if inclusive else '>=' }} {{ max_value }}{% endif %}
  )
{% endtest %}
