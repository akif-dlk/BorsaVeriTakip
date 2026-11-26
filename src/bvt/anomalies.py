"""Anormal fiyat tespitinin Python başvuru (referans) uygulaması.

`marts.mart_price_anomalies` (dbt, SQL) ile **aynı kuralları ve eşikleri** kullanır;
`tests/test_mart_anomalies_pg.py` ikisini aynı veriyle karşılaştırır, `tests/test_dbt_project.py` eşiklerin SQL'deki
Jinja ayarlarıyla aynı olduğunu doğrular.

Kurallar (her enstrüman kendi işlem günleri üzerinde, `adj_close` ile; satır elenmez, yalnızca raporlanır):

- `return_spike` (error): |basit getiri| varlık sınıfı eşiğinden büyük (fx/altın %10, endeks %15, hisse %25).
- `zscore_spike` (warn): log getirinin önceki 60 getiriye (bugün hariç, en az 20) göre z-skoru |z| > 5.
- `stale_price` (warn): aynı `adj_close` art arda en az 5 işlem günü (5. günden itibaren her gün işaretlenir).
- `zero_volume` (warn): hissede işlem günü hacim = 0.
- `invalid_ohlc` (warn): `has_valid_ohlc = false` (low ≤ open/close ≤ high bozuk); NULL bayrak sayılmaz.

Eşik gerekçeleri: BIST hisselerinde günlük fiyat marjı ±%10'dur; tatil sonrası getiri birkaç günü kapsayabildiği
için hisse eşiği iki tavan/taban gününe izin verir. Bu eşiği aşan hareket çoğunlukla düzeltilmemiş bölünme /
bedelsiz sermaye artırımı ya da kaynak hatasıdır. Döviz ve altında %10'luk günlük hareket (ör. Aralık 2021 USDTRY)
gerçek olabilir; kayıt yine `error` olarak raporlanır, gözden geçirme kararı insana bırakılır. z-skoru eşiği
oynaklığa göre ölçeklenir: sakin bir enstrümanda küçük ama olağandışı sıçramaları yakalar.
"""
from __future__ import annotations

import math

import pandas as pd

from bvt.indicators import log_returns

# Varlık sınıfı başına mutlak günlük basit getiri eşiği (oran; 0.10 = %10)
RETURN_SPIKE_THRESHOLDS = {"fx": 0.1, "gold": 0.1, "index": 0.15, "stock": 0.25}
ZSCORE_WINDOW = 60       # z-skoru için geçmiş getiri penceresi (bugün hariç)
ZSCORE_MIN_OBS = 20      # pencerede en az bu kadar getiri yoksa z-skoru hesaplanmaz
ZSCORE_THRESHOLD = 5
STALE_RUN_DAYS = 5       # aynı fiyat bu kadar işlem günü sürerse bayat
MIN_STD = 1e-12          # sabit seride kayan nokta gürültüsünü sıfır std saymak için

ANOMALY_SEVERITY = {
    "return_spike": "error",
    "zscore_spike": "warn",
    "stale_price": "warn",
    "zero_volume": "warn",
    "invalid_ohlc": "warn",
}
ANOMALY_TYPES = tuple(ANOMALY_SEVERITY)
ANOMALY_COLUMNS = ("instrument_code", "price_date", "asset_class", "anomaly_type", "severity", "metric_value",
                   "threshold", "adj_close", "prev_adj_close")


def _require(df: pd.DataFrame, columns: set[str]) -> None:
    missing = columns - set(df.columns)
    if missing:
        raise ValueError(f"eksik sütun(lar): {sorted(missing)}")


def _num(value) -> float:
    return math.nan if value is None or pd.isna(value) else float(value)


def _instrument_anomalies(group: pd.DataFrame) -> list[dict]:
    g = group.reset_index(drop=True)
    close = g["adj_close"].astype("float64")
    prev = close.shift(1)
    valid = (close > 0) & (prev > 0)
    simple = (close / prev - 1).where(valid)
    logr = log_returns(close)

    history = logr.shift(1).rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN_OBS)
    mean, std = history.mean(), history.std(ddof=1)
    zscore = ((logr - mean) / std).where(std > MIN_STD)

    new_level = prev.isna() | (close != prev)
    run_length = g.groupby(new_level.cumsum()).cumcount() + 1

    volume = g["volume"] if "volume" in g else pd.Series([None] * len(g))
    ohlc_ok = g["has_valid_ohlc"] if "has_valid_ohlc" in g else pd.Series([True] * len(g))

    rows: list[dict] = []
    for i, row in g.iterrows():
        def add(kind: str, metric, threshold) -> None:
            rows.append({
                "instrument_code": row["instrument_code"],
                "price_date": row["price_date"],
                "asset_class": row["asset_class"],
                "anomaly_type": kind,
                "severity": ANOMALY_SEVERITY[kind],
                "metric_value": _num(metric),
                "threshold": _num(threshold),
                "adj_close": float(close[i]),
                "prev_adj_close": _num(prev[i]),
            })

        limit = RETURN_SPIKE_THRESHOLDS.get(row["asset_class"])
        if limit is not None and not pd.isna(simple[i]) and abs(simple[i]) > limit:
            add("return_spike", simple[i], limit)
        if not pd.isna(zscore[i]) and abs(zscore[i]) > ZSCORE_THRESHOLD:
            add("zscore_spike", zscore[i], ZSCORE_THRESHOLD)
        if run_length[i] >= STALE_RUN_DAYS:
            add("stale_price", run_length[i], STALE_RUN_DAYS)
        if row["asset_class"] == "stock" and not pd.isna(volume[i]) and volume[i] == 0:
            add("zero_volume", 0, None)
        if not pd.isna(ohlc_ok[i]) and not bool(ohlc_ok[i]):  # NULL bayrak tutarsızlık sayılmaz
            add("invalid_ohlc", None, None)
    return rows


def detect_anomalies(prices: pd.DataFrame) -> pd.DataFrame:
    """`instrument_code, price_date, asset_class, adj_close` (+ isteğe bağlı `volume`, `has_valid_ohlc`)
    fiyatlarındaki anormallikler; her (enstrüman, gün, kural) için bir satır, ANOMALY_COLUMNS sütunlarıyla.

    Bir gün birden fazla kurala takılabilir (ör. hem `return_spike` hem `zscore_spike`).
    """
    _require(prices, {"instrument_code", "price_date", "asset_class", "adj_close"})
    rows: list[dict] = []
    for _, group in prices.sort_values(["instrument_code", "price_date"]).groupby("instrument_code", sort=True):
        rows.extend(_instrument_anomalies(group))
    result = pd.DataFrame(rows, columns=list(ANOMALY_COLUMNS))
    return result.sort_values(["instrument_code", "price_date", "anomaly_type"], ignore_index=True)


def summarize(anomalies: pd.DataFrame) -> dict[str, int]:
    """Kural başına anormallik sayısı (rapor/XCom için)."""
    counts = anomalies["anomaly_type"].value_counts().to_dict() if len(anomalies) else {}
    return {kind: int(counts.get(kind, 0)) for kind in ANOMALY_TYPES}
