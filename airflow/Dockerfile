# Airflow + proje bağımlılıkları. Kod (src/) imaja kopyalanmaz, docker-compose ile bağlanır.
ARG AIRFLOW_VERSION=2.10.5
FROM apache/airflow:${AIRFLOW_VERSION}-python3.11

ARG AIRFLOW_VERSION
COPY requirements.txt /tmp/bvt-requirements.txt
# Airflow sürümü sabitlenir ki proje bağımlılıkları onu yükseltip/düşürmesin.
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /tmp/bvt-requirements.txt
