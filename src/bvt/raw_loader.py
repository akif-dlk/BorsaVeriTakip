"""Raw katman için ortak, idempotent yükleyici.

Üç çekici (döviz, hisse, altın) aynı yükleme sözleşmesini kullanır:

1. Partide aynı doğal anahtar birden fazla kez geçiyorsa son satır kalır (Postgres `ON CONFLICT` aynı
   komutta bir satırı iki kez güncelleyemez).
2. Satırlar `COPY` ile geçici bir aşama (stage) tablosuna yazılır (executemany'den çok daha hızlı; uzun
   backfill'lerde önemli).
3. Tek bir `INSERT ... SELECT ... ON CONFLICT DO UPDATE` ile hedefe aktarılır. Güncelleme yalnızca değer
   gerçekten değiştiyse yapılır (`IS DISTINCT FROM`); aynı veri ikinci kez gelirse hiçbir satır yeniden
   yazılmaz, `fetched_at` değişmez, tabloda kopya oluşmaz.
4. `RETURNING (xmax = 0)` ile eklenen / güncellenen satırlar ayrılır; kalanlar "değişmedi" sayılır.

Sütun anlamları: `inserted_at` satırın ilk yüklendiği an, `fetched_at` değerlerin son yazıldığı an
(ilk ekleme ya da kaynak revizyonu, ör. Yahoo'nun geçmiş `adj_close` düzeltmesi).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pandas as pd


@dataclass(frozen=True)
class RawTable:
    """Bir raw tablonun yükleme sözleşmesi: doğal anahtar + üzerine yazılabilir değer sütunları."""

    name: str
    key: tuple[str, ...]
    values: tuple[str, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return (*self.key, *self.values)

    @property
    def stage(self) -> str:
        return "_stage_" + self.name.replace(".", "_")


FX_RATES = RawTable(
    "raw.fx_rates",
    key=("pair", "rate_date", "source"),
    values=("open", "high", "low", "close"),
)
STOCK_PRICES = RawTable(
    "raw.stock_prices",
    key=("symbol", "price_date", "source"),
    values=("open", "high", "low", "close", "adj_close", "volume"),
)
GOLD_PRICES = RawTable(
    "raw.gold_prices",
    key=("instrument", "price_date", "source"),
    values=("currency", "open", "high", "low", "close", "usdtry"),
)
RAW_TABLES: dict[str, RawTable] = {"fx": FX_RATES, "stocks": STOCK_PRICES, "gold": GOLD_PRICES}


@dataclass
class LoadResult:
    """Bir yüklemenin sonucu. `total` partideki (tekilleştirilmiş) satır sayısıdır."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def __add__(self, other: LoadResult) -> LoadResult:
        return LoadResult(self.inserted + other.inserted, self.updated + other.updated,
                          self.unchanged + other.unchanged)

    def __str__(self) -> str:
        return f"{self.inserted} yeni, {self.updated} güncellendi, {self.unchanged} değişmedi"


# --------------------------------------------------------------------------- SQL
def create_stage_sql(table: RawTable) -> str:
    # Tipler, NOT NULL ve varsayılanlar (fetched_at/inserted_at = now()) kopyalanır; CHECK kısıtları hedefte
    # denetlenir. Stage'e yalnızca sözleşme sütunları yazılır, zaman damgaları varsayılandan gelir.
    return f"CREATE TEMP TABLE {table.stage} (LIKE {table.name} INCLUDING DEFAULTS) ON COMMIT DROP"


def copy_sql(table: RawTable) -> str:
    return f"COPY {table.stage} ({', '.join(table.columns)}) FROM STDIN"


def upsert_sql(table: RawTable) -> str:
    cols = ", ".join(table.columns)
    sets = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c in table.values)
    current = ", ".join(f"t.{c}" for c in table.values)
    incoming = ", ".join(f"EXCLUDED.{c}" for c in table.values)
    return (
        f"INSERT INTO {table.name} AS t ({cols})\n"
        f"SELECT {cols} FROM {table.stage}\n"
        f"ON CONFLICT ({', '.join(table.key)}) DO UPDATE SET\n"
        f"    {sets},\n"
        f"    fetched_at = now()\n"
        f"WHERE ({current}) IS DISTINCT FROM ({incoming})\n"
        f"RETURNING (t.xmax = 0) AS inserted"
    )


def drop_stage_sql(table: RawTable) -> str:
    return f"DROP TABLE IF EXISTS {table.stage}"


# --------------------------------------------------------------------------- satır hazırlama
def to_python(value):
    """numpy/pandas skalerlerini psycopg'nin tanıdığı Python tiplerine çevirir; NaN/NaT -> None."""
    if value is None or value is pd.NaT or value is pd.NA:  # NaT bir datetime alt sınıfıdır, önce elenmeli
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, (str, bool, int, Decimal, date, datetime)):
        return value
    if hasattr(value, "item"):  # numpy skaler
        value = value.item()
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def dedupe(rows: Iterable[Sequence], key_len: int) -> list[tuple]:
    """Aynı anahtarlı satırlardan sonuncusunu tutar; ilk görülme sırası korunur."""
    out: dict[tuple, tuple] = {}
    for row in rows:
        row = tuple(to_python(v) for v in row)
        key = row[:key_len]
        if any(k is None for k in key):
            raise ValueError(f"Anahtar sütunu boş olamaz: {row}")
        out[key] = row
    return list(out.values())


def frame_rows(df: pd.DataFrame, table: RawTable) -> list[tuple]:
    missing = [c for c in table.columns if c not in df.columns]
    if missing:
        raise ValueError(f"{table.name} için eksik sütun(lar): {missing}")
    return list(df[list(table.columns)].itertuples(index=False, name=None))


# --------------------------------------------------------------------------- yükleme
def load_rows(conn, table: RawTable, rows: Iterable[Sequence], commit: bool = True) -> LoadResult:
    """Satırları `table`'a idempotent biçimde upsert eder ve sonucu döner.

    `commit=False` ise işlem çağıranın transaction'ında kalır (testlerde geri alınabilir).
    """
    batch = dedupe(rows, len(table.key))
    if not batch:
        return LoadResult()
    with conn.cursor() as cur:
        cur.execute(create_stage_sql(table))
        with cur.copy(copy_sql(table)) as cp:
            for row in batch:
                cp.write_row(row)
        cur.execute(upsert_sql(table))
        flags = [r[0] for r in cur.fetchall()]
        cur.execute(drop_stage_sql(table))
    if commit:
        conn.commit()
    inserted = sum(1 for f in flags if f)
    updated = len(flags) - inserted
    return LoadResult(inserted=inserted, updated=updated, unchanged=len(batch) - len(flags))


def load_frame(conn, table: RawTable, df: pd.DataFrame, commit: bool = True) -> LoadResult:
    """DataFrame'i (çekicilerin `clean()` çıktısı) `table`'a yükler."""
    if df is None or df.empty:
        return LoadResult()
    return load_rows(conn, table, frame_rows(df, table), commit=commit)
