-- Gün 13: gönderilen alarmların kaydı (bvt.alerts). Aynı sorun kümesi bekleme süresi (BVT_ALERT_COOLDOWN_HOURS)
-- dolmadan yeniden gönderilmez; kayıt aynı zamanda sağlık paneli (Gün 19) için alarm geçmişidir.
-- Tekrar çalıştırılabilir.
CREATE TABLE IF NOT EXISTS ops.alerts (
    alert_id   BIGSERIAL   PRIMARY KEY,
    dedup_key  TEXT        NOT NULL,                         -- aynı sorun kümesi → aynı anahtar
    kind       TEXT        NOT NULL,                         -- health_check | task_failed | test
    severity   TEXT        NOT NULL CHECK (severity IN ('error', 'warn', 'info')),
    title      TEXT        NOT NULL,
    body       TEXT        NOT NULL,
    channels   TEXT[]      NOT NULL DEFAULT '{}',            -- başarıyla gönderilen kanallar (telegram, email)
    errors     TEXT,                                         -- başarısız kanalların hata özeti
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_alerts_dedup_sent ON ops.alerts (dedup_key, sent_at DESC);
CREATE INDEX IF NOT EXISTS ix_alerts_sent ON ops.alerts (sent_at DESC);
