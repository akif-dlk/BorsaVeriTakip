"""dbt projesinin yapısal tutarlılığı (veritabanı ve dbt kurulumu gerektirmez).

Gerçek derleme: `python -m bvt.dbt_runner parse`; veritabanına karşı: `python -m bvt.dbt_runner build`.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
DBT = ROOT / "dbt"
STAGING = DBT / "models" / "staging"
# Üç stg_* modelinin ortak fiyat sözleşmesi (Gün 9'daki fact_daily_prices bunları birleştirir)
CONTRACT = ["instrument_code", "asset_class", "price_date", "source", "currency", "open", "high", "low", "close",
            "adj_close", "volume", "is_primary_source", "inserted_at", "loaded_at", "has_valid_ohlc"]
STAGING_MODELS = ["stg_fx_rates", "stg_gold_prices", "stg_stock_prices"]


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_project_and_profile_names_match():
    project, profiles = load(DBT / "dbt_project.yml"), load(DBT / "profiles.yml")
    assert project["profile"] in profiles
    assert project["models"]["bvt"]["staging"]["+schema"] == "staging"


def test_profile_reads_credentials_from_env():
    output = load(DBT / "profiles.yml")["bvt"]["outputs"]["dev"]
    for key in ("host", "port", "user", "password", "dbname"):
        assert "env_var(" in str(output[key]), key


def test_every_staging_model_is_documented_and_tested():
    documented = {m["name"]: m for m in load(STAGING / "_staging.yml")["models"]}
    assert sorted(p.stem for p in STAGING.glob("stg_*.sql")) == STAGING_MODELS
    assert sorted(documented) == STAGING_MODELS
    for name, model in documented.items():
        tests = [next(iter(t)) if isinstance(t, dict) else t for t in model.get("data_tests", [])]
        assert "unique_combination_of_columns" in tests, name
        cols = {c["name"] for c in model["columns"]}
        assert {"instrument_code", "price_date", "source", "close"} <= cols, name


@pytest.mark.parametrize("model", STAGING_MODELS)
def test_staging_models_share_price_contract(model):
    sql = (STAGING / f"{model}.sql").read_text(encoding="utf-8")
    aliases = set(re.findall(r"\bas\s+([a-z_]+)", sql)) | {"inserted_at"}  # inserted_at olduğu gibi seçilir
    missing = [c for c in CONTRACT if c not in aliases]
    assert not missing, f"{model}: eksik sütun(lar) {missing}"
    assert "inserted_at" in sql
    assert "source('raw'," in sql  # raw tablolara her zaman source() ile erişilir (lineage, freshness)


def test_sources_match_raw_tables_in_sql_init():
    declared = {t["name"] for t in load(STAGING / "_sources.yml")["sources"][0]["tables"]}
    created = set()
    for path in (ROOT / "sql" / "init").glob("*.sql"):
        created |= set(re.findall(r"CREATE TABLE IF NOT EXISTS raw\.(\w+)", path.read_text(encoding="utf-8")))
    assert declared == created


def test_source_has_freshness_on_fetched_at():
    source = load(STAGING / "_sources.yml")["sources"][0]
    assert source["schema"] == "raw" and source["loaded_at_field"] == "fetched_at"
    assert source["freshness"]["warn_after"]["count"] < source["freshness"]["error_after"]["count"]


def test_generic_test_and_macros_exist():
    generic = (DBT / "tests" / "generic" / "unique_combination_of_columns.sql").read_text(encoding="utf-8")
    assert "{% test unique_combination_of_columns" in generic
    assert "macro generate_schema_name" in (DBT / "macros" / "generate_schema_name.sql").read_text(encoding="utf-8")
    assert "macro ohlc_is_valid" in (DBT / "macros" / "ohlc_is_valid.sql").read_text(encoding="utf-8")
