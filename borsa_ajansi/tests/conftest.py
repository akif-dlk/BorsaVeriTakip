import os

# Testler kullanıcının .env ayarlarından bağımsız, sabit varsayılanlarla çalışır
# (load_dotenv mevcut ortam değişkenlerini ezmez; bu blok config import edilmeden önce çalışır).
os.environ.update({
    "MIN_DEBATE_ROUNDS": "2", "MAX_DEBATE_ROUNDS": "3", "MORNING_REPORT_ENABLED": "true",
    "SIM_INITIAL_CASH": "10000", "SIM_DAYS": "14", "SIM_CANDIDATES": "3", "SIM_MAX_POSITIONS": "4",
    "SIM_RISK_PER_TRADE_PCT": "2", "SIM_MAX_POSITION_PCT": "30", "SIM_COMMISSION_PCT": "0.2",
    "SIM_SLIPPAGE_PCT": "0.1", "SIM_TRADE_HOUR": "10", "SIM_TRADE_MINUTE": "30", "SIM_EOD_HOUR": "18",
    "SIM_EOD_MINUTE": "30", "UNIVERSE": "", "REPORT_HOUR": "9", "REPORT_MINUTE": "30",
})

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    """Deterministik 260 günlük sentetik fiyat serisi (yükselen trend + gürültü)."""
    rng = np.random.default_rng(42)
    n = 260
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = 100 + np.cumsum(rng.normal(0.2, 1.5, n))
    high = close + rng.uniform(0.2, 2.0, n)
    low = close - rng.uniform(0.2, 2.0, n)
    open_ = close + rng.normal(0, 0.5, n)
    vol = rng.integers(1_000_000, 2_000_000, n).astype(float)
    vol[-1] = vol[-21:-1].mean() * 3  # son gün hacim anomalisi
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
