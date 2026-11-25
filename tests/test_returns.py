"""bvt.returns: günlük getiri, haftalık/aylık özet, hareketli korelasyon başvuru uygulaması."""
import math
from datetime import date, timedelta

import pandas as pd
import pytest

from bvt import returns as rt


def _business_days(start: date, n: int) -> list[date]:
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _prices(closes, code="THYAO", start=date(2024, 1, 1), volume=None):
    df = pd.DataFrame({"instrument_code": code, "price_date": _business_days(start, len(closes)),
                       "adj_close": closes})
    if volume is not None:
        df["volume"] = volume
    return df


def _pearson(x, y):
    mx, my = sum(x) / len(x), sum(y) / len(y)
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return cov / math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))


WAVE = [100 * (1 + 0.04 * math.sin(i / 3) + 0.002 * i) for i in range(300)]


def test_daily_returns_definitions():
    out = rt.daily_returns(_prices([100.0, 110.0, 99.0, 0.0, 50.0]))
    assert out.obs_index.tolist() == [1, 2, 3, 4, 5]
    assert math.isnan(out.simple_return[0]) and math.isnan(out.log_return[0])
    assert out.simple_return[1] == pytest.approx(0.10) and out.simple_return[2] == pytest.approx(-0.10)
    assert out.log_return[1] == pytest.approx(math.log(1.1))
    assert math.isnan(out.simple_return[3]) and math.isnan(out.simple_return[4])  # fiyat ≤ 0 → NULL
    assert out.cumulative_return.tolist()[:3] == pytest.approx([0.0, 0.10, -0.01])


def test_daily_returns_gap_days_and_prev_date():
    out = rt.daily_returns(_prices([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], start=date(2024, 1, 3)))  # Çar-Çar
    assert out.prev_price_date[0] is None and out.prev_price_date[1] == date(2024, 1, 3)
    assert out.days_since_prev.tolist()[1:] == [1, 1, 3, 1, 1]  # Cuma → Pazartesi = 3
    assert out.days_since_prev.isna().tolist()[0]


def test_daily_returns_is_per_instrument_and_order_independent():
    df = pd.concat([_prices(WAVE[:30], "THYAO"), _prices([v * 2 for v in WAVE[:10]], "USDTRY")])
    out = rt.daily_returns(df.sample(frac=1, random_state=3))
    usd = out[out.instrument_code == "USDTRY"]
    assert usd.obs_index.tolist() == list(range(1, 11)) and usd.price_date.is_monotonic_increasing
    assert usd.cumulative_return.iloc[0] == 0.0  # her seri kendi ilk fiyatına göre
    thy = out[out.instrument_code == "THYAO"]
    assert thy.log_return.iloc[1:].tolist() == pytest.approx([math.log(WAVE[i] / WAVE[i - 1]) for i in range(1, 30)])


def test_daily_returns_validates_and_handles_empty():
    with pytest.raises(ValueError, match="adj_close"):
        rt.daily_returns(pd.DataFrame({"instrument_code": [], "price_date": []}))
    empty = rt.daily_returns(_prices([]))
    assert empty.empty and set(rt.RETURN_COLUMNS) <= set(empty.columns)


@pytest.mark.parametrize("day,ptype,start,end", [
    (date(2024, 1, 3), "week", date(2024, 1, 1), date(2024, 1, 7)),
    (date(2024, 1, 7), "week", date(2024, 1, 1), date(2024, 1, 7)),       # pazar haftanın son günü
    (date(2024, 2, 15), "month", date(2024, 2, 1), date(2024, 2, 29)),    # artık yıl
    (date(2024, 12, 31), "month", date(2024, 12, 1), date(2024, 12, 31)),
])
def test_period_bounds(day, ptype, start, end):
    assert rt.period_start(day, ptype) == start
    assert rt.period_end(start, ptype) == end


def test_period_bounds_reject_unknown_type():
    with pytest.raises(ValueError, match="dönem"):
        rt.period_start(date(2024, 1, 1), "year")
    with pytest.raises(ValueError, match="dönem"):
        rt.period_end(date(2024, 1, 1), "quarter")


def test_weekly_summary_values():
    # 2024-01-01 Pzt'den 10 iş günü: iki tam hafta
    closes = [100.0, 102.0, 101.0, 103.0, 104.0, 105.0, 103.0, 106.0, 107.0, 110.0]
    r = rt.daily_returns(_prices(closes, volume=[10] * 10))
    s = rt.period_summary(r, today=date(2024, 6, 1))
    weeks = s[s.period_type == "week"].reset_index(drop=True)
    assert weeks.period_start_date.tolist() == [date(2024, 1, 1), date(2024, 1, 8)]
    assert weeks.period_end_date.tolist() == [date(2024, 1, 7), date(2024, 1, 14)]
    assert weeks.n_trading_days.tolist() == [5, 5]
    assert weeks.first_close.tolist() == [100.0, 105.0] and weeks.last_close.tolist() == [104.0, 110.0]
    assert weeks.max_close.tolist() == [104.0, 110.0] and weeks.min_close.tolist() == [100.0, 103.0]
    assert math.isnan(weeks.period_return[0]) and math.isnan(weeks.prev_period_close[0])
    assert weeks.period_return[1] == pytest.approx(110 / 104 - 1)
    assert weeks.period_log_return[1] == pytest.approx(math.log(110 / 104))
    # 2. hafta getirisi = o haftanın günlük log getirilerinin toplamı (1. günün getirisi önceki haftadan başlar)
    second = r[[d >= date(2024, 1, 8) for d in r.price_date]]
    assert weeks.period_log_return[1] == pytest.approx(second.log_return.sum())
    expected_vol = second.log_return.std(ddof=1) * math.sqrt(252)
    assert weeks.volatility[1] == pytest.approx(expected_vol)
    assert weeks.total_volume.tolist() == [50.0, 50.0]
    assert weeks.is_complete.all()


def test_monthly_summary_and_completeness():
    r = rt.daily_returns(_prices(WAVE[:45], start=date(2024, 1, 1)))  # Ocak + Şubat + Mart başı
    s = rt.period_summary(r, today=date(2024, 3, 1))
    months = s[s.period_type == "month"].reset_index(drop=True)
    assert months.period_start_date.tolist() == [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)]
    assert months.n_trading_days.tolist() == [23, 21, 1]
    assert months.is_complete.tolist() == [True, True, False]  # Mart sürüyor
    assert math.isnan(months.volatility[2])  # tek getiri: std tanımsız
    assert months.total_volume.isna().all()  # hacim sütunu yok
    assert months.period_return[1] == pytest.approx(months.last_close[1] / months.last_close[0] - 1)


def test_period_summary_previous_period_skips_empty_periods():
    df = pd.DataFrame({"instrument_code": "XU100", "adj_close": [100.0, 120.0],
                       "price_date": [date(2024, 1, 31), date(2024, 3, 1)]})  # Şubat'ta veri yok
    months = rt.period_summary(rt.daily_returns(df), today=date(2025, 1, 1)).query("period_type == 'month'")
    assert months.prev_period_close.tolist()[1] == 100.0
    assert months.period_return.tolist()[1] == pytest.approx(0.2)


def test_period_summary_empty():
    out = rt.period_summary(rt.daily_returns(_prices([])))
    assert out.empty and list(out.columns) == list(rt.PERIOD_COLUMNS)


def test_rolling_correlation_matches_pearson_on_common_days():
    a = _prices(WAVE[:120], "USDTRY")
    b = _prices([w * (1 + 0.01 * math.cos(i)) for i, w in enumerate(WAVE[:120])], "THYAO")
    b = b.drop(index=[10, 11, 50])  # BIST tatili: ortak günler azalır
    out = rt.rolling_correlations(rt.daily_returns(pd.concat([a, b])), windows=(20, 60))
    assert set(zip(out.instrument_a, out.instrument_b)) == {("THYAO", "USDTRY")}  # bayt sırası, tek yön
    ra = rt.daily_returns(a).set_index("price_date").log_return
    rb = rt.daily_returns(b).set_index("price_date").log_return.dropna()
    common = [d for d in rb.index if not math.isnan(ra[d])]
    assert out.price_date.tolist() == common
    assert out.pair_obs_index.tolist() == list(range(1, len(common) + 1))
    assert out.corr_20d.iloc[:19].isna().all() and out.corr_20d.iloc[19:].notna().all()
    last = common[-20:]
    assert out.corr_20d.iloc[-1] == pytest.approx(_pearson([rb[d] for d in last], [ra[d] for d in last]))
    assert out.corr_60d.iloc[-1] == pytest.approx(
        _pearson([rb[d] for d in common[-60:]], [ra[d] for d in common[-60:]]))


def test_rolling_correlation_edge_cases():
    up = _prices(WAVE[:80], "AAA")
    same = _prices(WAVE[:80], "BBB")
    inverse = _prices([1 / w for w in WAVE[:80]], "CCC")
    flat = _prices([5.0] * 80, "DDD")
    out = rt.rolling_correlations(rt.daily_returns(pd.concat([up, same, inverse, flat])), windows=(60,))
    last = out.groupby(["instrument_a", "instrument_b"]).corr_60d.last()
    assert last[("AAA", "BBB")] == pytest.approx(1.0) and last[("AAA", "BBB")] <= 1.0
    assert last[("AAA", "CCC")] == pytest.approx(-1.0) and last[("AAA", "CCC")] >= -1.0
    assert math.isnan(last[("AAA", "DDD")])  # sabit seri: korelasyon tanımsız
    assert len(last) == 6  # 4 enstrüman → C(4, 2) çift


def test_rolling_correlation_no_overlap_and_empty():
    a = _prices(WAVE[:10], "AAA", start=date(2024, 1, 1))
    b = _prices(WAVE[:10], "BBB", start=date(2025, 1, 1))
    assert rt.rolling_correlations(rt.daily_returns(pd.concat([a, b]))).empty
    empty = rt.rolling_correlations(rt.daily_returns(_prices([])))
    assert empty.empty and list(empty.columns) == list(rt.CORRELATION_COLUMNS)


def test_correlation_matrix_is_symmetric_with_latest_values():
    corr = pd.DataFrame({
        "instrument_a": ["AAA", "AAA", "AAA", "BBB"],
        "instrument_b": ["BBB", "BBB", "CCC", "CCC"],
        "price_date": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 2), date(2024, 1, 2)],
        "corr_60d": [0.1, 0.5, -0.3, math.nan],
    })
    m = rt.correlation_matrix(corr, window=60)
    assert list(m.index) == list(m.columns) == ["AAA", "BBB", "CCC"]
    assert (pd.Series([m.loc[c, c] for c in m.index]) == 1.0).all()
    assert m.loc["AAA", "BBB"] == m.loc["BBB", "AAA"] == 0.5
    assert m.loc["CCC", "AAA"] == -0.3
    assert math.isnan(m.loc["BBB", "CCC"])  # hiç dolu değer yok
    assert rt.correlation_matrix(corr, window=60, as_of=date(2024, 1, 1)).loc["AAA", "BBB"] == 0.1
