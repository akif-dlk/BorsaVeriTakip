# Airflow + proje bağımlılıkları. Kod (src/, dbt/) imaja kopyalanmaz, docker-compose ile bağlanır.
ARG AIRFLOW_VERSION=2.10.5
FROM apache/airflow:${AIRFLOW_VERSION}-python3.11

ARG AIRFLOW_VERSION
COPY requirements.txt /tmp/bvt-requirements.txt
COPY requirements-dbt.txt /tmp/bvt-requirements-dbt.txt
# Airflow sürümü sabitlenir ki proje bağımlılıkları onu yükseltip/düşürmesin.
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /tmp/bvt-requirements.txt

# dbt ayrı sanal ortamda: Airflow'un sabitlediği paketlerle (protobuf, jsonschema...) çakışmaz.
# bvt.dbt_runner bu ortamı DBT_BIN üzerinden alt süreç olarak çağırır.
USER root
RUN python -m venv /opt/dbt-venv \
    && /opt/dbt-venv/bin/pip install --no-cache-dir -r /tmp/bvt-requirements-dbt.txt \
    && chmod -R a+rX /opt/dbt-venv
USER airflow
ENV DBT_BIN=/opt/dbt-venv/bin/dbt
