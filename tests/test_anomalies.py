"""bvt.anomalies: anormal fiyat kurallarının Python başvuru uygulaması."""
import math
from datetime import date, timedelta

import pandas as pd
import pytest

from bvt import anomalies as an


def series(code: str, closes: list[float], asset_class: str = "stock", volume=None, ohlc=None,
           start: date = date(2024, 1, 1)) -> pd.DataFrame:
    days, day = [], start
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return pd.DataFrame({
        "instrument_code": code, "price_date": days, "asset_class": asset_class, "adj_close": closes,
        "volume": volume if volume is not None else [1000] * len(closes),
        "has_valid_ohlc": ohlc if ohlc is not None else [True] * len(closes),
    })


def wiggle(n: int, base: float = 100.0, amp: float = 0.01) -> list[float]:
    """Hiç tekrar etmeyen, küçük oynaklıklı fiyat serisi."""
    return [round(base * (1 + amp * math.sin(i * 1.7) + 0.0003 * i), 6) for i in range(n)]


def kinds(df: pd.DataFrame) -> list[tuple[str, str]]:
    return list(zip(df.instrument_code, df.anomaly_type))


def test_clean_series_has_no_anomalies():
    result = an.detect_anomalies(series("THYAO", wiggle(120)))
    assert result.empty
    assert list(result.columns) == list(an.ANOMALY_COLUMNS)
    assert an.summarize(result) == {k: 0 for k in an.ANOMALY_TYPES}


@pytest.mark.parametrize("asset_class, jump, flagged", [
    ("stock", 0.24, False), ("stock", 0.30, True), ("fx", 0.11, True), ("fx", -0.09, False),
    ("index", 0.16, True), ("gold", -0.12, True),
])
def test_return_spike_uses_asset_class_threshold(asset_class, jump, flagged):
    closes = wiggle(10)
    closes[5] = round(closes[4] * (1 + jump), 6)
    closes[6:] = [round(c * (1 + jump), 6) for c in closes[6:]]  # seviye kalıcı: yalnızca bir sıçrama
    result = an.detect_anomalies(series("X", closes, asset_class))
    spikes = result[result.anomaly_type == "return_spike"]
    assert len(spikes) == (1 if flagged else 0)
    if flagged:
        row = spikes.iloc[0]
        assert row.severity == "error"
        assert row.threshold == an.RETURN_SPIKE_THRESHOLDS[asset_class]
        assert row.metric_value == pytest.approx(jump, rel=1e-4)
        assert row.prev_adj_close == closes[4]


def test_unknown_asset_class_has_no_return_threshold():
    closes = wiggle(5)
    closes[3] = closes[2] * 3
    assert "return_spike" not in an.detect_anomalies(series("X", closes, "crypto")).anomaly_type.tolist()


def test_zscore_spike_needs_history_and_scales_with_volatility():
    closes = wiggle(80, amp=0.002)  # çok sakin seri
    jump_day = 70
    closes[jump_day:] = [c * 1.04 for c in closes[jump_day:]]  # %4: eşik altı getiri ama çok büyük z
    result = an.detect_anomalies(series("USDTRY", closes, "fx"))
    z = result[result.anomaly_type == "zscore_spike"]
    assert z.price_date.tolist() == [series("USDTRY", closes).price_date[jump_day]]
    assert z.iloc[0].metric_value > an.ZSCORE_THRESHOLD and z.iloc[0].severity == "warn"
    assert "return_spike" not in result.anomaly_type.tolist()


def test_zscore_not_computed_before_min_observations():
    closes = wiggle(an.ZSCORE_MIN_OBS, amp=0.002)  # en fazla MIN_OBS-1 geçmiş getiri
    closes[-1] = closes[-2] * 1.05
    assert "zscore_spike" not in an.detect_anomalies(series("X", closes, "fx")).anomaly_type.tolist()


def test_stale_price_flags_from_fifth_identical_day():
    closes = wiggle(5) + [42.0] * 7 + wiggle(3, base=43)
    result = an.detect_anomalies(series("X", closes))
    stale = result[result.anomaly_type == "stale_price"]
    # 42.0 seviyesinin 5., 6. ve 7. günü (index 9, 10, 11)
    assert stale.metric_value.tolist() == [5.0, 6.0, 7.0]
    assert (stale.threshold == an.STALE_RUN_DAYS).all()
    assert stale.adj_close.tolist() == [42.0] * 3


def test_stale_run_restarts_after_price_change():
    closes = [10.0] * 4 + [11.0] + [10.0] * 4
    assert "stale_price" not in an.detect_anomalies(series("X", closes)).anomaly_type.tolist()


def test_zero_volume_only_for_stocks():
    volume = [1000, 0, 500, 0]
    stock = an.detect_anomalies(series("THYAO", wiggle(4), "stock", volume=volume))
    assert stock[stock.anomaly_type == "zero_volume"].shape[0] == 2
    index = an.detect_anomalies(series("XU100", wiggle(4), "index", volume=volume))
    assert "zero_volume" not in index.anomaly_type.tolist()


def test_missing_volume_is_not_zero_volume():
    df = series("THYAO", wiggle(4), volume=[None, None, 10, None])
    assert an.detect_anomalies(df).empty


def test_invalid_ohlc_flag_and_null_flag():
    df = series("X", wiggle(4), ohlc=[True, False, None, True])
    result = an.detect_anomalies(df)
    assert kinds(result) == [("X", "invalid_ohlc")]
    assert math.isnan(result.iloc[0].metric_value) and math.isnan(result.iloc[0].threshold)


def test_optional_columns_and_multiple_instruments():
    a = series("B", wiggle(6)).drop(columns=["volume", "has_valid_ohlc"])
    b = series("A", [5.0] * 6, "fx").drop(columns=["volume", "has_valid_ohlc"])
    result = an.detect_anomalies(pd.concat([a, b]))
    assert kinds(result) == [("A", "stale_price"), ("A", "stale_price")]
    assert an.summarize(result)["stale_price"] == 2


def test_one_day_can_trigger_several_rules():
    closes = wiggle(40, amp=0.002)
    closes[35] = closes[34] * 1.5
    result = an.detect_anomalies(series("X", closes, "stock", volume=[100] * 35 + [0] + [100] * 4))
    day = result[result.price_date == result.price_date.iloc[0]]
    assert {"return_spike", "zscore_spike", "zero_volume"} <= set(day.anomaly_type)


def test_missing_columns_raise():
    with pytest.raises(ValueError, match="asset_class"):
        an.detect_anomalies(pd.DataFrame({"instrument_code": [], "price_date": [], "adj_close": []}))


def test_severity_map_covers_every_rule():
    assert set(an.ANOMALY_SEVERITY.values()) == {"error", "warn"}
    assert an.ANOMALY_SEVERITY["return_spike"] == "error"
