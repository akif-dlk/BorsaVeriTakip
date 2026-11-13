-- Ham BIST fiyatları: hisseler + BIST 100 endeksi, (sembol, tarih, kaynak) başına tek satır.
-- Sembol `.IS` soneki olmadan tutulur (THYAO, XU100). `close` ham kapanış; `adj_close` temettü/bölünme
-- düzeltmeli kapanış (getiri hesaplarında bu kullanılır). Endekslerde hacim 0 veya NULL olabilir.
CREATE TABLE IF NOT EXISTS raw.stock_prices (
    symbol      TEXT          NOT NULL,              -- THYAO, GARAN, ..., XU100
    price_date  DATE          NOT NULL,
    source      TEXT          NOT NULL,              -- yfinance
    open        NUMERIC(18,6),
    high        NUMERIC(18,6),
    low         NUMERIC(18,6),
    close       NUMERIC(18,6) NOT NULL CHECK (close > 0),
    adj_close   NUMERIC(18,6),
    volume      BIGINT        CHECK (volume >= 0),
    fetched_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, price_date, source)
);

CREATE INDEX IF NOT EXISTS ix_stock_prices_date ON raw.stock_prices (price_date);
