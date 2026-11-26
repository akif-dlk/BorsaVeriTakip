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
MARTS = DBT / "models" / "marts"
SEED = DBT / "seeds" / "instrument_metadata.csv"
# Üç stg_* modelinin ortak fiyat sözleşmesi (Gün 9'daki fact_daily_prices bunları birleştirir)
CONTRACT = ["instrument_code", "asset_class", "price_date", "source", "currency", "open", "high", "low", "close",
            "adj_close", "volume", "is_primary_source", "inserted_at", "loaded_at", "has_valid_ohlc"]
STAGING_MODELS = ["stg_fx_rates", "stg_gold_prices", "stg_stock_prices"]
MART_MODELS = ["dim_date", "dim_instrument", "fact_daily_prices", "mart_correlations", "mart_indicators",
               "mart_period_returns", "mart_price_anomalies", "mart_returns"]
SINGULAR_TESTS = ["assert_fact_covers_primary_prices", "assert_gram_gold_matches_ounce", "warn_recent_price_anomalies"]


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_project_and_profile_names_match():
    project, profiles = load(DBT / "dbt_project.yml"), load(DBT / "profiles.yml")
    assert project["profile"] in profiles
    assert project["models"]["bvt"]["staging"]["+schema"] == "staging"
    assert project["models"]["bvt"]["marts"] == {**project["models"]["bvt"]["marts"],
                                                 "+schema": "marts", "+materialized": "table"}


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
    assert "{% test accepted_range" in (DBT / "tests" / "generic" / "accepted_range.sql").read_text(encoding="utf-8")
    assert "{% test not_in_future" in (DBT / "tests" / "generic" / "not_in_future.sql").read_text(encoding="utf-8")
    assert "macro generate_schema_name" in (DBT / "macros" / "generate_schema_name.sql").read_text(encoding="utf-8")
    assert "macro ohlc_is_valid" in (DBT / "macros" / "ohlc_is_valid.sql").read_text(encoding="utf-8")


def test_every_mart_model_is_documented():
    documented = {m["name"]: m for m in load(MARTS / "_marts.yml")["models"]}
    assert sorted(p.stem for p in MARTS.glob("*.sql")) == MART_MODELS
    assert sorted(documented) == MART_MODELS
    for name in MART_MODELS:
        sql = (MARTS / f"{name}.sql").read_text(encoding="utf-8")
        assert "source(" not in sql, f"{name}: marts raw'a değil staging'e (ref) bağlanır"
        # Son select listesindeki her çıktı sütunu (takma ad ya da `t.sütun`) belgelenmiş olmalı
        documented_cols = {c["name"] for c in documented[name]["columns"]}
        aliases = _output_columns(sql)
        assert aliases, name
        assert aliases <= documented_cols, f"{name}: belgelenmemiş {aliases - documented_cols}"


def _output_columns(sql: str) -> set[str]:
    """Modelin son `select ... from` listesindeki çıktı sütun adları (satır başına bir ifade varsayımıyla)."""
    select_list = sql.rsplit("\nselect", 1)[1].split("\nfrom ", 1)[0]
    names = set()
    for line in select_list.splitlines():
        line = line.split("--", 1)[0].strip().rstrip(",")
        alias = re.search(r"\bas\s+([a-z_][a-z0-9_]*)$", line)
        bare = re.fullmatch(r"(?:[a-z_]+\.)?([a-z_][a-z0-9_]*)", line)
        if alias or bare:
            names.add((alias or bare).group(1))
    return names


def _column_tests(model: dict, column: str) -> list:
    col = next(c for c in model["columns"] if c["name"] == column)
    return [next(iter(t)) if isinstance(t, dict) else t for t in col.get("data_tests", [])]


def test_dimension_keys_are_unique_and_not_null():
    models = {m["name"]: m for m in load(MARTS / "_marts.yml")["models"]}
    for model, key in [("dim_instrument", "instrument_key"), ("dim_instrument", "instrument_code"),
                       ("dim_date", "date_key"), ("dim_date", "date_day")]:
        assert {"unique", "not_null"} <= set(_column_tests(models[model], key)), (model, key)


def test_fact_grain_and_foreign_keys_are_tested():
    fact = next(m for m in load(MARTS / "_marts.yml")["models"] if m["name"] == "fact_daily_prices")
    grain = fact["data_tests"][0]["unique_combination_of_columns"]["combination_of_columns"]
    assert grain == ["instrument_key", "date_key"]
    for key, dim in [("instrument_key", "dim_instrument"), ("date_key", "dim_date")]:
        col = next(c for c in fact["columns"] if c["name"] == key)
        rel = next(t["relationships"] for t in col["data_tests"] if isinstance(t, dict) and "relationships" in t)
        assert rel == {"to": f"ref('{dim}')", "field": key}
    sql = (MARTS / "fact_daily_prices.sql").read_text(encoding="utf-8")
    for model in STAGING_MODELS:
        assert f"ref('{model}') }}}} where is_primary_source" in sql, model  # yalnızca birincil kaynak


def test_seed_covers_every_configured_instrument():
    import csv

    from bvt import config

    with SEED.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    codes = [r["instrument_code"] for r in rows]
    assert len(codes) == len(set(codes)), "seed'de tekrarlanan enstrüman"
    expected = {*config.FX_PAIRS, *config.bist_symbols(), *config.GOLD_INSTRUMENTS}
    assert set(codes) == expected
    by_code = {r["instrument_code"]: r for r in rows}
    assert {by_code[c]["asset_class"] for c in config.FX_PAIRS} == {"fx"}
    assert by_code[config.BIST_INDEX.removesuffix(".IS")]["asset_class"] == "index"
    assert by_code["XAUUSD_OZ"]["currency"] == "USD"
    assert all(r["instrument_name"].strip() for r in rows)


def test_seed_schema_is_documented():
    seed = load(DBT / "seeds" / "_seeds.yml")["seeds"][0]
    assert seed["name"] == SEED.stem
    with SEED.open(encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
    assert header == list(seed["config"]["column_types"])


def test_surrogate_key_macro_exists():
    macro = (DBT / "macros" / "surrogate_key.sql").read_text(encoding="utf-8")
    assert "macro surrogate_key" in macro and "md5(" in macro


def test_mart_indicators_matches_python_reference_definitions():
    """SQL'deki pencere/periyot ayarları ve çıktı sütunları bvt.indicators ile aynı olmalı."""
    from bvt import indicators as ind

    sql = (MARTS / "mart_indicators.sql").read_text(encoding="utf-8")
    settings = dict(re.findall(r"{%-? set (\w+) = (.+?) %}", sql))
    assert settings == {"sma_windows": str(list(ind.SMA_WINDOWS)), "ema_spans": str(list(ind.EMA_SPANS)),
                        "rsi_window": str(ind.RSI_WINDOW), "vol_window": str(ind.VOLATILITY_WINDOW),
                        "trading_days": str(ind.TRADING_DAYS_PER_YEAR)}
    assert set(ind.INDICATOR_COLUMNS) | {"rsi_zone", "obs_index"} <= _output_columns(sql)
    assert f">= {ind.RSI_OVERBOUGHT} then 'overbought'" in sql and f"<= {ind.RSI_OVERSOLD} then 'oversold'" in sql
    assert "ref('fact_daily_prices')" in sql  # göstergeler star schema olgusundan (birincil kaynak) türetilir


def test_mart_indicators_grain_and_ranges_are_tested():
    model = next(m for m in load(MARTS / "_marts.yml")["models"] if m["name"] == "mart_indicators")
    grain = model["data_tests"][0]["unique_combination_of_columns"]["combination_of_columns"]
    assert grain == ["instrument_key", "date_key"]
    rsi = next(c for c in model["columns"] if c["name"] == "rsi_14")
    assert {"accepted_range": {"min_value": 0, "max_value": 100}} in rsi["data_tests"]
    for col in ("sma_20", "sma_50", "sma_200", "ema_12", "ema_26", "volatility_20d"):
        assert "accepted_range" in _column_tests(model, col), col


def _model(name: str) -> dict:
    return next(m for m in load(MARTS / "_marts.yml")["models"] if m["name"] == name)


def test_return_marts_match_python_reference_definitions():
    """Getiri/korelasyon modellerindeki ayarlar ve çıktı sütunları bvt.returns ile aynı olmalı."""
    from bvt import indicators as ind
    from bvt import returns as rt

    returns_sql = (MARTS / "mart_returns.sql").read_text(encoding="utf-8")
    assert "ref('fact_daily_prices')" in returns_sql
    assert set(rt.RETURN_COLUMNS) <= _output_columns(returns_sql)

    periods_sql = (MARTS / "mart_period_returns.sql").read_text(encoding="utf-8")
    settings = dict(re.findall(r"{%-? set (\w+) = (.+?) %}", periods_sql))
    assert settings == {"periods": str(list(rt.PERIOD_TYPES)), "trading_days": str(ind.TRADING_DAYS_PER_YEAR)}
    assert set(rt.PERIOD_COLUMNS) <= _output_columns(periods_sql)

    corr_sql = (MARTS / "mart_correlations.sql").read_text(encoding="utf-8")
    settings = dict(re.findall(r"{%-? set (\w+) = (.+?) %}", corr_sql))
    assert settings == {"windows": str(list(rt.CORRELATION_WINDOWS))}
    assert {"instrument_a", "instrument_b", "price_date", "pair_obs_index"} <= _output_columns(corr_sql)
    assert 'collate "C"' in corr_sql  # çift sırası Python'daki sorted() ile aynı (yerel ayardan bağımsız)
    for model in ("mart_period_returns", "mart_correlations"):
        assert "ref('mart_returns')" in (MARTS / f"{model}.sql").read_text(encoding="utf-8"), model
    documented = {c["name"] for c in _model("mart_correlations")["columns"]}
    assert {f"corr_{n}d" for n in rt.CORRELATION_WINDOWS} <= documented


def test_return_marts_grain_and_ranges_are_tested():
    grains = {
        "mart_returns": ["instrument_key", "date_key"],
        "mart_period_returns": ["instrument_key", "period_type", "period_start_date"],
        "mart_correlations": ["instrument_a_key", "instrument_b_key", "date_key"],
    }
    for name, grain in grains.items():
        model = _model(name)
        assert model["data_tests"][0]["unique_combination_of_columns"]["combination_of_columns"] == grain, name
    corr = _model("mart_correlations")
    for col in ("corr_60d", "corr_252d"):
        assert {"accepted_range": {"min_value": -1, "max_value": 1}} in next(
            c for c in corr["columns"] if c["name"] == col)["data_tests"], col
    for key in ("instrument_a_key", "instrument_b_key"):
        assert "relationships" in _column_tests(corr, key), key
    assert "accepted_values" in _column_tests(_model("mart_period_returns"), "period_type")
    assert "accepted_range" in _column_tests(_model("mart_returns"), "simple_return")


def test_mart_price_anomalies_matches_python_reference_definitions():
    """Anormallik eşikleri ve kural adları SQL'de bvt.anomalies ile aynı olmalı."""
    from bvt import anomalies as an

    sql = (MARTS / "mart_price_anomalies.sql").read_text(encoding="utf-8")
    settings = dict(re.findall(r"{%-? set (\w+) = (.+?) %}", sql))
    assert settings == {"return_thresholds": str(an.RETURN_SPIKE_THRESHOLDS), "zscore_window": str(an.ZSCORE_WINDOW),
                        "zscore_min_obs": str(an.ZSCORE_MIN_OBS), "zscore_threshold": str(an.ZSCORE_THRESHOLD),
                        "stale_run_days": str(an.STALE_RUN_DAYS), "min_std": str(an.MIN_STD)}
    for kind, severity in an.ANOMALY_SEVERITY.items():
        assert re.search(rf"'{kind}'(?: as anomaly_type)?, '{severity}'", sql), kind
    assert set(an.ANOMALY_COLUMNS) <= _output_columns(sql)
    assert "ref('fact_daily_prices')" in sql
    model = _model("mart_price_anomalies")
    accepted = next(t["accepted_values"]["values"] for t in next(
        c for c in model["columns"] if c["name"] == "anomaly_type")["data_tests"] if isinstance(t, dict))
    assert accepted == list(an.ANOMALY_TYPES)
    grain = model["data_tests"][0]["unique_combination_of_columns"]["combination_of_columns"]
    assert grain == ["instrument_key", "date_key", "anomaly_type"]


def test_singular_tests_exist_and_use_ref():
    found = sorted(p.stem for p in (DBT / "tests").glob("*.sql"))
    assert found == SINGULAR_TESTS
    for name in SINGULAR_TESTS:
        sql = (DBT / "tests" / f"{name}.sql").read_text(encoding="utf-8")
        assert "ref('" in sql and "source(" not in sql, name
    warn = (DBT / "tests" / "warn_recent_price_anomalies.sql").read_text(encoding="utf-8")
    assert "config(severity='warn')" in warn  # geçmiş şoklar günlük build'i kırmaz


def test_marts_singular_tests_do_not_depend_on_staging():
    """dbt build eager seçimde bir ebeveyni seçilen testi çalıştırır: staging build'inde eski marts tablolarına
    karşı yanlış alarm vermesin diye marts testleri staging modellerine ref vermez."""
    for name in ("assert_fact_covers_primary_prices", "warn_recent_price_anomalies"):
        sql = (DBT / "tests" / f"{name}.sql").read_text(encoding="utf-8")
        assert "ref('stg_" not in sql, name


def test_fact_prices_positive_and_dates_not_in_future():
    fact = _model("fact_daily_prices")
    for col in ("open", "high", "low", "close", "adj_close"):
        assert {"accepted_range": {"min_value": 0, "inclusive": False}} in next(
            c for c in fact["columns"] if c["name"] == col)["data_tests"], col
    assert "not_in_future" in _column_tests(fact, "price_date")
    staging = {m["name"]: m for m in load(STAGING / "_staging.yml")["models"]}
    for name, model in staging.items():
        assert "not_in_future" in _column_tests(model, "price_date"), name
        assert "accepted_range" in _column_tests(model, "adj_close"), name


def test_ops_dbt_test_results_table_matches_report_statuses():
    from bvt import dbt_report

    sql = (ROOT / "sql" / "init" / "007_ops_dbt_test_results.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ops.dbt_test_results" in sql
    statuses = re.search(r"status IN \(([^)]*)\)", sql).group(1)
    assert [v.strip(" '") for v in statuses.split(",")] == list(dbt_report.STATUSES)
    assert "PRIMARY KEY (invocation_id, unique_id)" in sql
