-- Gün 5: raw katman sözleşmesi ve idempotent yükleme için ek alanlar.
-- Tüm ifadeler tekrar çalıştırılabilir (IF NOT EXISTS); yeni ve mevcut veritabanlarında aynı sonucu verir.
--
-- Raw sözleşmesi (bkz. src/bvt/raw_loader.py):
--   * Doğal anahtar (enstrüman, tarih, kaynak) PRIMARY KEY'dir; aynı veri iki kez gelirse kopya oluşmaz.
--   * Değer değişmediyse satır yeniden yazılmaz; değiştiyse (kaynak revizyonu) üzerine yazılır.
--   * inserted_at: satırın ilk yüklendiği an; fetched_at: değerlerin son yazıldığı an.

ALTER TABLE raw.fx_rates     ADD COLUMN IF NOT EXISTS inserted_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE raw.stock_prices ADD COLUMN IF NOT EXISTS inserted_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE raw.gold_prices  ADD COLUMN IF NOT EXISTS inserted_at TIMESTAMPTZ NOT NULL DEFAULT now();

COMMENT ON COLUMN raw.fx_rates.inserted_at     IS 'Satırın ilk yüklendiği an';
COMMENT ON COLUMN raw.fx_rates.fetched_at      IS 'Değerlerin son yazıldığı an (ekleme veya kaynak revizyonu)';
COMMENT ON COLUMN raw.stock_prices.inserted_at IS 'Satırın ilk yüklendiği an';
COMMENT ON COLUMN raw.stock_prices.fetched_at  IS 'Değerlerin son yazıldığı an (ekleme veya kaynak revizyonu)';
COMMENT ON COLUMN raw.gold_prices.inserted_at  IS 'Satırın ilk yüklendiği an';
COMMENT ON COLUMN raw.gold_prices.fetched_at   IS 'Değerlerin son yazıldığı an (ekleme veya kaynak revizyonu)';

-- Pipeline çalışmalarında ekleme/güncelleme/değişmeyen ayrımı ve istenen tarih penceresi
ALTER TABLE ops.pipeline_runs
    ADD COLUMN IF NOT EXISTS rows_inserted  INTEGER,
    ADD COLUMN IF NOT EXISTS rows_updated   INTEGER,
    ADD COLUMN IF NOT EXISTS rows_unchanged INTEGER,
    ADD COLUMN IF NOT EXISTS window_start   DATE,
    ADD COLUMN IF NOT EXISTS window_end     DATE;

COMMENT ON COLUMN ops.pipeline_runs.rows_loaded IS 'Partideki (tekilleştirilmiş) satır sayısı = inserted + updated + unchanged';

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_source_started ON ops.pipeline_runs (source, started_at DESC);
