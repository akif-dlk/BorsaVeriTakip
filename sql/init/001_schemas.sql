-- Katmanlar: raw (kaynaktan geldiği gibi), staging/marts (dbt), streaming (Kafka consumer)
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS streaming;
CREATE SCHEMA IF NOT EXISTS ops;

-- Pipeline çalışma kayıtları (sağlık paneli ve bayat veri alarmı için)
CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    source      TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL DEFAULT 'running',
    rows_loaded INTEGER,
    error       TEXT
);
