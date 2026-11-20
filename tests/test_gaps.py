from datetime import date, timedelta

import pytest

from bvt import config, gaps
from bvt.gaps import MARKET_CLOSED, MISSING, Gap

# 2026-11-16 Pazartesi .. 2026-11-22 Pazar
MON = date(2026, 11, 16)
WEEK = [MON + timedelta(days=i) for i in range(7)]


def test_weekdays_skip_weekend():
    assert gaps.weekdays(WEEK[0], WEEK[6]) == WEEK[:5]
    assert gaps.weekdays(WEEK[5], WEEK[6]) == []
    with pytest.raises(ValueError):
        gaps.weekdays(WEEK[1], WEEK[0])


def test_holiday_vs_missing():
    # Çarşamba hiçbir enstrümanda yok -> tatil; Perşembe yalnızca B'de yok -> gerçek eksik
    observed = {"A": {WEEK[0], WEEK[1], WEEK[3], WEEK[4]}, "B": {WEEK[0], WEEK[1], WEEK[4]}}
    first = {"A": date(2020, 1, 1), "B": date(2020, 1, 1)}
    found = gaps.find_gaps("stocks", observed, first, WEEK[0], WEEK[6], ["A", "B"])
    assert found == [Gap("stocks", WEEK[2], "*", MARKET_CLOSED), Gap("stocks", WEEK[3], "B", MISSING)]


def test_days_before_first_seen_are_not_gaps():
    observed = {"A": set(WEEK[:5]), "NEW": {WEEK[3], WEEK[4]}}
    first = {"A": date(2020, 1, 1), "NEW": WEEK[3]}
    assert gaps.find_gaps("stocks", observed, first, WEEK[0], WEEK[6], ["A", "NEW"]) == []


def test_instrument_never_seen_is_reported_in_log_not_gaps(caplog):
    observed = {"A": set(WEEK[:5])}
    found = gaps.find_gaps("fx", observed, {"A": date(2020, 1, 1)}, WEEK[0], WEEK[4], ["A", "B"])
    assert found == []
    assert "B" in caplog.text and "backfill" in caplog.text


def test_empty_database_produces_no_gaps():
    assert gaps.find_gaps("gold", {}, {}, WEEK[0], WEEK[4], list(config.GOLD_INSTRUMENTS)) == []


def test_gold_gram_missing_when_ounce_present():
    oz, gram = config.GOLD_INSTRUMENTS
    observed = {oz: set(WEEK[:5]), gram: set(WEEK[:4])}
    first = {oz: date(2020, 1, 1), gram: date(2020, 1, 1)}
    assert gaps.find_gaps("gold", observed, first, WEEK[0], WEEK[4]) == [Gap("gold", WEEK[4], gram, MISSING)]


def test_expected_instruments():
    assert gaps.expected_instruments("fx") == ["USDTRY", "EURTRY"]
    assert "XU100" in gaps.expected_instruments("stocks") and "THYAO" in gaps.expected_instruments("stocks")
    assert gaps.expected_instruments("gold") == ["XAUUSD_OZ", "XAUTRY_GRAM"]
    with pytest.raises(ValueError):
        gaps.expected_instruments("crypto")


def test_merge_windows():
    d = date(2026, 1, 5)
    days = [d, d + timedelta(days=1), d + timedelta(days=5), d + timedelta(days=30), d]
    assert gaps.merge_windows(days, max_gap_days=7) == [(d, d + timedelta(days=5)),
                                                         (d + timedelta(days=30), d + timedelta(days=30))]
    assert gaps.merge_windows([]) == []


def test_summarize():
    found = [Gap("fx", WEEK[0], "*", MARKET_CLOSED), Gap("fx", WEEK[1], "EURTRY", MISSING),
             Gap("fx", WEEK[2], "EURTRY", MISSING)]
    assert gaps.summarize(found) == {MISSING: 2, MARKET_CLOSED: 1, "missing_by_instrument": {"EURTRY": 2}}


class _Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append((sql, params))

    def executemany(self, sql, rows):
        self.conn.log.append((sql, list(rows)))


class GapConn:
    def __init__(self, window_rows, first_rows):
        self.window_rows, self.first_rows, self.log, self.commits = window_rows, first_rows, [], 0

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        return _Res(self.window_rows if params else self.first_rows)

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1


def test_check_source_reads_db_and_saves_snapshot():
    window = [("USDTRY", d) for d in WEEK[:5]] + [("EURTRY", d) for d in WEEK[:4]]
    conn = GapConn(window, [("USDTRY", date(2020, 1, 1)), ("EURTRY", date(2020, 1, 1))])
    found = gaps.check_source(conn, "fx", WEEK[0], WEEK[6])
    assert found == [Gap("fx", WEEK[4], "EURTRY", MISSING)]
    sqls = [s for s, _ in conn.log]
    assert "source = 'yfinance'" in sqls[0]
    assert sqls[2].startswith("DELETE FROM ops.data_gaps") and conn.log[2][1] == ("fx", WEEK[0], WEEK[6])
    assert conn.log[3][1] == [("fx", "EURTRY", WEEK[4], MISSING)]
    assert conn.commits == 1


def test_check_source_no_save():
    conn = GapConn([], [])
    assert gaps.check_source(conn, "stocks", WEEK[0], WEEK[4], save=False) == []
    assert conn.commits == 0 and not any("ops.data_gaps" in s for s, _ in conn.log)
