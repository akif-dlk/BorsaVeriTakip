-- Gün 12: dbt test sonuçları (bvt.dbt_report). Her `dbt build` çağrısının test sonuçları run_results.json'dan
-- okunup buraya yazılır; günlük test raporu ve alarm (Gün 13) bu tabloyu okur. Tekrar çalıştırılabilir.
CREATE TABLE IF NOT EXISTS ops.dbt_test_results (
    invocation_id    TEXT             NOT NULL,             -- dbt çağrısının kimliği
    unique_id        TEXT             NOT NULL,             -- test.bvt.<ad>.<hash>
    generated_at     TIMESTAMPTZ      NOT NULL,             -- run_results.json oluşturulma anı
    test_name        TEXT             NOT NULL,
    test_type        TEXT             NOT NULL,             -- not_null, unique, accepted_range ... veya singular
    model            TEXT,                                  -- testin bağlı olduğu model / kaynak
    column_name      TEXT,
    severity         TEXT             NOT NULL CHECK (severity IN ('error', 'warn')),
    status           TEXT             NOT NULL CHECK (status IN ('pass', 'warn', 'fail', 'error', 'skipped')),
    failures         INTEGER,                               -- testi bozan satır sayısı
    message          TEXT,
    execution_time_s DOUBLE PRECISION,
    recorded_at      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (invocation_id, unique_id)
);
CREATE INDEX IF NOT EXISTS ix_dbt_test_results_generated ON ops.dbt_test_results (generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_dbt_test_results_status ON ops.dbt_test_results (status, generated_at DESC);
