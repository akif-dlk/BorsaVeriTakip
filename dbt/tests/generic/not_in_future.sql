{#-
  Tarih sütununun İstanbul takvimine göre bugünden ileride olmadığını doğrular (yanlış saat dilimi, tarih
  ayrıştırma hatası). Bağlantının saat dilimi UTC olsa da gün İstanbul'a göre hesaplanır; NULL test dışıdır.
-#}
{% test not_in_future(model, column_name, tolerance_days=0) %}
select {{ column_name }}
from {{ model }}
where {{ column_name }} > (now() at time zone 'Europe/Istanbul')::date + {{ tolerance_days }}
{% endtest %}
