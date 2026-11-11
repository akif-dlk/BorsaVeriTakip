from bvt import config


def test_pg_dsn_defaults(monkeypatch):
    for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB"):
        monkeypatch.delenv(k, raising=False)
    assert config.pg_dsn() == "postgresql://bvt:bvt@localhost:5432/borsa"


def test_bist_tickers_have_suffix():
    assert all(t.endswith(".IS") for t in config.BIST_TICKERS)
