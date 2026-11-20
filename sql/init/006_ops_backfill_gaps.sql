-- Gün 7: çalışma tetikleyicisi, devam ettirilebilir backfill ve veri boşluğu kayıtları.
-- Tüm ifadeler tekrar çalıştırılabilir (IF NOT EXISTS).

-- Kim başlattı: manual (CLI), airflow (günlük DAG), backfill (geçmiş yükleme), repair (boşluk onarımı)
ALTER TABLE ops.pipeline_runs ADD COLUMN IF NOT EXISTS run_trigger TEXT NOT NULL DEFAULT 'manual';
COMMENT ON COLUMN ops.pipeline_runs.run_trigger IS 'manual | airflow | backfill | repair';

-- Backfill kaldığı yerden devam ederken başarılı pencereleri bu indeksle arar
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_source_status_window
    ON ops.pipeline_runs (source, status, window_start, window_end);

-- Veri boşlukları (bvt.gaps). Her kontrol, kaynak + tarih aralığı için anlık görüntüyü yeniden yazar:
-- onarılan boşluk bir sonraki kontrolde kaybolur.
--   market_closed: o iş günü kaynağın hiçbir enstrümanında veri yok (resmî tatil / piyasa kapalı). instrument = '*'
--   missing      : aynı gün diğer enstrümanlarda veri var, bu enstrümanda yok (gerçek eksik; onarılmalı)
CREATE TABLE IF NOT EXISTS ops.data_gaps (
    source      TEXT        NOT NULL,
    instrument  TEXT        NOT NULL,
    gap_date    DATE        NOT NULL,
    kind        TEXT        NOT NULL CHECK (kind IN ('market_closed', 'missing')),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, instrument, gap_date)
);
CREATE INDEX IF NOT EXISTS ix_data_gaps_kind_date ON ops.data_gaps (kind, gap_date DESC);
