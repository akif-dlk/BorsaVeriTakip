-- Ham döviz kurları: kaynaktan geldiği gibi, (parite, tarih, kaynak) başına tek satır.
-- yfinance OHLC verir; EVDS yalnızca gösterge kur (close) verir, diğer alanlar NULL kalır.
CREATE TABLE IF NOT EXISTS raw.fx_rates (
    pair        TEXT          NOT NULL,              -- USDTRY, EURTRY
    rate_date   DATE          NOT NULL,
    source      TEXT          NOT NULL,              -- yfinance | evds
    open        NUMERIC(18,6),
    high        NUMERIC(18,6),
    low         NUMERIC(18,6),
    close       NUMERIC(18,6) NOT NULL CHECK (close > 0),
    fetched_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (pair, rate_date, source)
);

CREATE INDEX IF NOT EXISTS ix_fx_rates_date ON raw.fx_rates (rate_date);
