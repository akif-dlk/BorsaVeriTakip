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
