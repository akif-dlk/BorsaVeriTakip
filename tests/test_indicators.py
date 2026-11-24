"""bvt.indicators: SMA, EMA, RSI (Wilder), volatilite başvuru uygulaması."""
import math
from datetime import date, timedelta

import pandas as pd
import pytest

from bvt import indicators as ind


def _prices(closes, code="THYAO", start=date(2024, 1, 1)):
    return pd.DataFrame({
        "instrument_code": code,
        "price_date": [start + timedelta(days=i) for i in range(len(closes))],
        "adj_close": closes,
    })


def _wilder_rsi_loop(closes, window=14):
    """Bağımsız döngü uygulaması (karşılaştırma için)."""
    out, avg_gain, avg_loss = [None], None, None
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if avg_gain is None:
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain = gain / window + (1 - 1 / window) * avg_gain
            avg_loss = loss / window + (1 - 1 / window) * avg_loss
        if i < window:
            out.append(None)
        elif avg_loss == 0:
            out.append(50.0 if avg_gain == 0 else 100.0)
        else:
            out.append(100 - 100 / (1 + avg_gain / avg_loss))
    return out


ZIGZAG = [100 + 5 * math.sin(i / 3) + 0.3 * i for i in range(260)]


def test_sma_is_null_until_window_full():
    s = ind.sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)
    assert s.isna().tolist() == [True, True, False, False]
    assert s.iloc[2:].tolist() == [2.0, 3.0]


def test_ema_seeded_with_first_close_and_warmup_nulls():
    closes = [10.0, 11.0, 12.0, 13.0]
    s = ind.ema(pd.Series(closes), 3)  # α = 0.5
    expected, e = [], closes[0]
    for c in closes:
        e = 0.5 * c + 0.5 * e
        expected.append(e)
    assert s.isna().tolist() == [True, True, False, False]
    assert s.iloc[2:].tolist() == pytest.approx(expected[2:])


def test_rsi_matches_independent_wilder_loop():
    got = ind.rsi(pd.Series(ZIGZAG)).tolist()
    want = _wilder_rsi_loop(ZIGZAG)
    assert [math.isnan(g) for g in got] == [w is None for w in want]
    assert [g for g in got if not math.isnan(g)] == pytest.approx([w for w in want if w is not None])
    assert all(0 <= g <= 100 for g in got if not math.isnan(g))


def test_rsi_edge_cases():
    up = ind.rsi(pd.Series([float(i) for i in range(1, 31)]))
    assert up.iloc[:14].isna().all() and (up.iloc[14:] == 100.0).all()
    flat = ind.rsi(pd.Series([5.0] * 30))
    assert (flat.iloc[14:] == 50.0).all()
    down = ind.rsi(pd.Series([float(i) for i in range(30, 0, -1)]))
    assert (down.iloc[14:] == 0.0).all()


def test_volatility_is_annualized_sample_std_of_log_returns():
    closes = ZIGZAG[:40]
    vol = ind.volatility(pd.Series(closes), 20)
    assert vol.iloc[:20].isna().all() and vol.iloc[20:].notna().all()  # 20 getiri = 21 fiyat
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, 21)]
    mean = sum(rets) / 20
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / 19)
    assert vol.iloc[20] == pytest.approx(std * math.sqrt(252))


def test_log_return_skips_non_positive_prices():
    r = ind.log_returns(pd.Series([1.0, 2.0, 0.0, 3.0]))
    assert math.isnan(r.iloc[0]) and r.iloc[1] == pytest.approx(math.log(2))
    assert math.isnan(r.iloc[2]) and math.isnan(r.iloc[3])


@pytest.mark.parametrize("value,zone", [(None, None), (math.nan, None), (70.0, "overbought"), (69.9, "neutral"),
                                        (30.0, "oversold"), (55.0, "neutral")])
def test_rsi_zone(value, zone):
    assert ind.rsi_zone(value) == zone


def test_compute_indicators_per_instrument_and_columns():
    df = pd.concat([_prices(ZIGZAG, "THYAO"), _prices([v * 2 for v in ZIGZAG[:30]], "USDTRY")])
    out = ind.compute_indicators(df.sample(frac=1, random_state=1))  # sıra karışık gelse de
    assert set(ind.INDICATOR_COLUMNS) | {"obs_index", "rsi_zone"} <= set(out.columns)
    usd = out[out.instrument_code == "USDTRY"]
    assert usd.obs_index.tolist() == list(range(1, 31))
    assert usd.price_date.is_monotonic_increasing
    assert usd.sma_200.isna().all() and usd.sma_20.notna().sum() == 11  # seriler birbirine karışmaz
    thy = out[out.instrument_code == "THYAO"]
    assert thy.sma_200.notna().sum() == len(ZIGZAG) - 199
    assert thy.rsi_14.tolist()[14:] == pytest.approx(_wilder_rsi_loop(ZIGZAG)[14:])


def test_compute_indicators_validates_and_handles_empty():
    with pytest.raises(ValueError, match="adj_close"):
        ind.compute_indicators(pd.DataFrame({"instrument_code": [], "price_date": []}))
    empty = ind.compute_indicators(_prices([]))
    assert empty.empty and set(ind.INDICATOR_COLUMNS) <= set(empty.columns)
