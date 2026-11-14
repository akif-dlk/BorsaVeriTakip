-- Ham altın fiyatları: (enstrüman, tarih, kaynak) başına tek satır.
--   XAUUSD_OZ   : ons altın, USD (yfinance GC=F, COMEX vadeli sürekli kontrat)  source = 'yfinance'
--   XAUTRY_GRAM : gram altın, TRY = ons × USDTRY / 31.1034768                     source = 'derived'
-- Gram satırlarında `usdtry` dönüşümde kullanılan kuru tutar (izlenebilirlik); ons satırlarında NULL.
CREATE TABLE IF NOT EXISTS raw.gold_prices (
    instrument  TEXT          NOT NULL,              -- XAUUSD_OZ | XAUTRY_GRAM
    price_date  DATE          NOT NULL,
    source      TEXT          NOT NULL,              -- yfinance | derived
    currency    TEXT          NOT NULL,              -- USD | TRY
    open        NUMERIC(18,6),
    high        NUMERIC(18,6),
    low         NUMERIC(18,6),
    close       NUMERIC(18,6) NOT NULL CHECK (close > 0),
    usdtry      NUMERIC(18,6) CHECK (usdtry > 0),
    fetched_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument, price_date, source)
);

CREATE INDEX IF NOT EXISTS ix_gold_prices_date ON raw.gold_prices (price_date);
