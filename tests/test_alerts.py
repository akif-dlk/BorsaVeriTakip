"""bvt.alerts birim testleri: bayatlık hesabı, mesaj, kanallar (sahte HTTP/SMTP), tekrar önleme, Airflow geri çağırma."""
import smtplib
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from bvt import alerts
from bvt.alerts import Alert, AlertDeliveryError, EmailNotifier, Problem, TelegramNotifier

TOKEN = "123456:SECRET-TOKEN"


# -------------------------------------------------------------------------------------------------- bayatlık


@pytest.mark.parametrize("last, ref, expected", [
    (date(2026, 11, 27), date(2026, 11, 27), 0),   # aynı gün
    (date(2026, 11, 28), date(2026, 11, 27), 0),   # ileride (saat dilimi farkı)
    (date(2026, 11, 26), date(2026, 11, 27), 1),   # Per → Cum
    (date(2026, 11, 27), date(2026, 11, 30), 1),   # Cum → Pzt: hafta sonu sayılmaz
    (date(2026, 11, 27), date(2026, 11, 29), 0),   # Cum → Paz
    (date(2026, 11, 20), date(2026, 11, 27), 5),
])
def test_business_days_after(last, ref, expected):
    assert alerts.business_days_after(last, ref) == expected


def test_stale_problems_threshold_and_missing_instruments():
    ref = date(2026, 11, 27)  # Cuma
    expected = {"fx": ["USDTRY", "EURTRY"], "stocks": ["THYAO"], "gold": []}
    last = {("fx", "USDTRY"): date(2026, 11, 27),
            ("fx", "EURTRY"): date(2026, 11, 25),   # 2 iş günü: eşikte, bayat değil
            ("stocks", "THYAO"): date(2026, 11, 24),  # 3 iş günü: bayat
            ("stocks", "OLDSYM"): date(2026, 11, 27)}  # config dışı ama veride var: değerlendirilir
    problems = alerts.stale_problems(last, ref, max_lag=2, expected=expected)
    assert [p.subject for p in problems] == ["stocks/THYAO"]
    assert "3 iş günü" in problems[0].detail and problems[0].severity == "error"

    expected["gold"] = ["XAUTRY_GRAM"]
    problems = alerts.stale_problems(last, ref, max_lag=2, expected=expected)
    assert ("gold/XAUTRY_GRAM", "hiç veri yok") in [(p.subject, p.detail) for p in problems]


def test_stale_after_days_env(monkeypatch):
    monkeypatch.setenv("BVT_STALE_AFTER_DAYS", "4")
    assert alerts.stale_after_days() == 4
    assert alerts.stale_after_days(1) == 1
    monkeypatch.setenv("BVT_STALE_AFTER_DAYS", "x")
    with pytest.raises(ValueError):
        alerts.stale_after_days()


def test_expected_instruments_follow_config():
    exp = alerts.expected_instruments()
    assert exp["fx"] == ["USDTRY", "EURTRY"]
    assert "XU100" in exp["stocks"] and "THYAO" in exp["stocks"]
    assert exp["gold"] == ["XAUUSD_OZ", "XAUTRY_GRAM"]


# ------------------------------------------------------------------------------------------------------ mesaj


def test_alert_text_html_order_and_escaping():
    problems = (Problem("warn", "missing_days", "fx/EURTRY", "1 gün eksik"),
                Problem("error", "dbt_test", "<fact>", "fail & 3 satır"))
    alert = Alert("health_check", "Başlık <x>", problems, "alt bilgi")
    text = alert.text()
    assert text.index("dbt_test") < text.index("missing_days")  # error önce
    assert alert.severity == "error"
    body = alert.html()
    assert "<b>Başlık &lt;x&gt;</b>" in body and "&lt;fact&gt;" in body and "fail &amp; 3" in body
    assert "<fact>" not in body


def test_alert_lists_at_most_n_problems():
    problems = tuple(Problem("warn", "missing_days", f"s/{i:03}", "x") for i in range(alerts.MAX_LISTED_PROBLEMS + 5))
    assert "… ve 5 sorun daha" in Alert("health_check", "t", problems).text()


def test_problems_key_ignores_detail_and_order():
    a = [Problem("error", "stale_data", "fx/USDTRY", "3 gün"), Problem("warn", "missing_days", "x", "1")]
    b = [Problem("warn", "missing_days", "x", "2"), Problem("error", "stale_data", "fx/USDTRY", "4 gün")]
    assert alerts.problems_key("health", a) == alerts.problems_key("health", b)
    assert alerts.problems_key("health", a) != alerts.problems_key("health", a[:1])


def test_alert_severity_info_without_problems():
    assert Alert("test", "deneme").severity == "info"


# --------------------------------------------------------------------------------------------------- Telegram


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("json yok")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_telegram_sends_html_message():
    session = FakeSession([FakeResponse(200, {"ok": True})])
    TelegramNotifier(TOKEN, "42", session=session).send(Alert("test", "Merhaba <b>"))
    url, payload, timeout = session.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"
    assert payload["text"].startswith("<b>Merhaba &lt;b&gt;</b>") and timeout


def test_telegram_truncates_long_message():
    session = FakeSession([FakeResponse(200, {"ok": True})])
    long_problems = tuple(Problem("error", "dbt_test", "m" * 190, "d" * 190) for _ in range(30))
    TelegramNotifier(TOKEN, "42", session=session).send(Alert("health_check", "t", long_problems))
    assert len(session.calls[0][1]["text"]) <= alerts.TELEGRAM_MAX_CHARS


def test_telegram_retries_rate_limit_and_server_errors():
    session = FakeSession([FakeResponse(429), FakeResponse(502), FakeResponse(200, {"ok": True})])
    sleeps = []
    TelegramNotifier(TOKEN, "42", session=session, sleep=sleeps.append).send(Alert("test", "t"))
    assert len(session.calls) == 3 and len(sleeps) == 2


def test_telegram_client_error_is_not_retried():
    session = FakeSession([FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})])
    with pytest.raises(AlertDeliveryError, match="chat not found"):
        TelegramNotifier(TOKEN, "42", session=session, sleep=lambda s: None).send(Alert("test", "t"))
    assert len(session.calls) == 1


def test_telegram_network_error_redacts_token():
    import requests

    err = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    session = FakeSession([err, err])
    with pytest.raises(AlertDeliveryError) as info:
        TelegramNotifier(TOKEN, "42", session=session, attempts=2, sleep=lambda s: None).send(Alert("test", "t"))
    assert TOKEN not in str(info.value) and "***" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


# ----------------------------------------------------------------------------------------------------- e-posta


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, fail_login=False):
        self.host, self.port, self.timeout = host, port, timeout
        self.events, self.sent = [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.events.append("starttls")

    def login(self, user, password):
        if password == "wrong":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials for wrong")
        self.events.append(("login", user))

    def send_message(self, msg):
        self.sent.append(msg)


def test_email_starttls_login_and_message():
    FakeSMTP.instances.clear()
    notifier = EmailNotifier("smtp.example.com", 587, "bvt@example.com", ["a@example.com", "b@example.com"],
                             user="bvt@example.com", password="pw", smtp_factory=FakeSMTP)
    notifier.send(Alert("health_check", "BVT veri alarmı", (Problem("error", "stale_data", "fx/USDTRY", "x"),)))
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.events == ["starttls", ("login", "bvt@example.com")]
    msg = smtp.sent[0]
    assert msg["Subject"] == "BVT veri alarmı" and msg["To"] == "a@example.com, b@example.com"
    assert "fx/USDTRY" in msg.get_content()


def test_email_ssl_port_skips_starttls_and_errors_redact_password():
    FakeSMTP.instances.clear()
    EmailNotifier("h", 465, "f@x", ["t@x"], smtp_factory=FakeSMTP).send(Alert("test", "t"))
    assert FakeSMTP.instances[0].events == []  # 465: SSL bağlantı, STARTTLS yok, kullanıcı yoksa login yok

    bad = EmailNotifier("h", 587, "f@x", ["t@x"], user="u", password="wrong", smtp_factory=FakeSMTP)
    with pytest.raises(AlertDeliveryError) as info:
        bad.send(Alert("test", "t"))
    assert "wrong" not in str(info.value)


# ------------------------------------------------------------------------------------------ kanal yapılandırması


def test_notifiers_from_env():
    assert alerts.notifiers_from_env({}) == []
    both = alerts.notifiers_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42",
                                      "SMTP_HOST": "smtp.x", "SMTP_PORT": "465", "SMTP_USER": "u@x",
                                      "ALERT_EMAIL_TO": "a@x, b@x", "SMTP_STARTTLS": "false"})
    assert [n.name for n in both] == ["telegram", "email"]
    email = both[1]
    assert email.port == 465 and email.recipients == ["a@x", "b@x"] and email.sender == "u@x"
    assert email.starttls is False


def test_notifiers_from_env_skips_half_configured(caplog):
    assert alerts.notifiers_from_env({"TELEGRAM_BOT_TOKEN": TOKEN}) == []
    assert alerts.notifiers_from_env({"SMTP_HOST": "smtp.x"}) == []
    assert "eksik yapılandırılmış" in caplog.text
    with pytest.raises(ValueError):
        alerts.notifiers_from_env({"SMTP_HOST": "h", "ALERT_EMAIL_TO": "a@x", "SMTP_PORT": "abc"})


class StubNotifier:
    def __init__(self, name, error=None):
        self.name, self.error, self.sent = name, error, []

    def send(self, alert):
        if self.error:
            raise self.error
        self.sent.append(alert)


def test_dispatch_isolates_channel_failures():
    ok, bad = StubNotifier("email"), StubNotifier("telegram", AlertDeliveryError("HTTP 401"))
    results = alerts.dispatch(Alert("test", "t"), [bad, ok])
    assert results == {"telegram": "HTTP 401", "email": "ok"}
    assert len(ok.sent) == 1


def test_dispatch_without_channels_logs(caplog):
    assert alerts.dispatch(Alert("test", "yalnızca log"), []) == {}
    assert "yalnızca log" in caplog.text


# ------------------------------------------------------------------------------------- tekrar önleme ve kayıt


class FakeAlertConn:
    """ops.alerts sorgularını taklit eder: RECENT_SQL için `recent` döner, INSERT'leri kaydeder."""

    def __init__(self, recent=False):
        self.recent, self.inserted, self.commits, self.queries = recent, [], 0, []

    def execute(self, sql, params=()):
        self.queries.append(sql)
        if sql == alerts.RECENT_SQL:
            return SimpleNamespace(fetchall=lambda: [(1,)] if self.recent else [])
        if sql == alerts.INSERT_SQL:
            self.inserted.append(params)
        return SimpleNamespace(fetchall=lambda: [])

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


NOW = datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)


def test_send_with_dedup_sends_and_records():
    conn, notifier = FakeAlertConn(), StubNotifier("telegram")
    alert = Alert("health_check", "t", (Problem("error", "stale_data", "fx/USDTRY", "x"),), dedup_key="health:k")
    result = alerts.send_with_dedup(alert, [notifier], NOW, 12, conn)
    assert result == {"status": "sent", "channels": {"telegram": "ok"}}
    key, kind, severity, title, body, channels, errors = conn.inserted[0]
    assert (key, kind, severity, channels, errors) == ("health:k", "health_check", "error", ["telegram"], None)


def test_send_with_dedup_suppresses_within_cooldown():
    conn, notifier = FakeAlertConn(recent=True), StubNotifier("telegram")
    result = alerts.send_with_dedup(Alert("health_check", "t", dedup_key="k"), [notifier], NOW, 12, conn)
    assert result["status"] == "suppressed" and notifier.sent == [] and conn.inserted == []


def test_send_with_dedup_zero_cooldown_always_sends():
    conn, notifier = FakeAlertConn(recent=True), StubNotifier("telegram")
    assert alerts.send_with_dedup(Alert("t", "t", dedup_key="k"), [notifier], NOW, 0, conn)["status"] == "sent"
    assert alerts.RECENT_SQL not in conn.queries


def test_send_with_dedup_reports_failed_and_logged():
    conn = FakeAlertConn()
    failed = alerts.send_with_dedup(Alert("t", "t", dedup_key="k"),
                                    [StubNotifier("telegram", AlertDeliveryError("401"))], NOW, 12, conn)
    assert failed["status"] == "failed" and conn.inserted[0][5] == [] and "401" in conn.inserted[0][6]
    conn = FakeAlertConn()
    assert alerts.send_with_dedup(Alert("t", "t", dedup_key="k"), [], NOW, 12, conn)["status"] == "logged"
    assert conn.inserted == []  # kanal yoksa kayıt da yok (hiçbir şey gönderilmedi)


def test_send_with_dedup_fails_open_when_db_unreachable(monkeypatch):
    from bvt import db

    def broken_connect(attempts=3):
        raise OSError("bağlantı reddedildi")

    monkeypatch.setattr(db, "connect", broken_connect)
    notifier = StubNotifier("telegram")
    result = alerts.send_with_dedup(Alert("t", "t", dedup_key="k"), [notifier], NOW, 12)
    assert result["status"] == "sent" and len(notifier.sent) == 1


# -------------------------------------------------------------------------------------------- check_and_alert


@pytest.fixture
def fake_db(monkeypatch):
    from bvt import db

    conn = FakeAlertConn()

    @contextmanager
    def connect(attempts=3):
        yield conn

    monkeypatch.setattr(db, "connect", connect)
    return conn


def test_check_and_alert_clean(fake_db, monkeypatch):
    monkeypatch.setattr(alerts, "collect_problems", lambda conn, ref, now: [])
    notifier = StubNotifier("telegram")
    result = alerts.check_and_alert(datetime(2026, 11, 27, 16, 30, tzinfo=timezone.utc), [notifier], NOW)
    assert result == {"ref_date": "2026-11-27", "problems": 0, "errors": 0, "warnings": 0, "status": "ok"}
    assert notifier.sent == []


def test_check_and_alert_sends_digest(fake_db, monkeypatch):
    seen = {}

    def collect(conn, ref, now):
        seen["ref"] = ref
        return [Problem("error", "stale_data", "stocks/THYAO", "x"), Problem("warn", "missing_days", "fx/EURTRY", "y")]

    monkeypatch.setattr(alerts, "collect_problems", collect)
    notifier = StubNotifier("telegram")
    # 21:30 UTC Cuma = 00:30 Cumartesi İstanbul: referans gün piyasa saat dilimine göre hesaplanır
    result = alerts.check_and_alert(datetime(2026, 11, 27, 21, 30, tzinfo=timezone.utc), [notifier], NOW, cooldown=12)
    assert seen["ref"] == date(2026, 11, 28)
    assert result["status"] == "sent" and result["errors"] == 1 and result["warnings"] == 1
    alert = notifier.sent[0]
    assert alert.kind == "health_check" and "1 hata, 1 uyarı" in alert.title
    assert alert.dedup_key.startswith("health:") and fake_db.inserted


def test_check_and_alert_dry_run_does_not_send(fake_db, monkeypatch, capsys):
    monkeypatch.setattr(alerts, "collect_problems",
                        lambda conn, ref, now: [Problem("error", "stale_data", "fx/USDTRY", "x")])
    notifier = StubNotifier("telegram")
    result = alerts.check_and_alert(date(2026, 11, 27), [notifier], NOW, dry_run=True)
    assert result["status"] == "dry_run" and notifier.sent == []
    assert "fx/USDTRY" in capsys.readouterr().out


# -------------------------------------------------------------------------------------------- collect_problems


class CollectConn:
    """collect_problems'in sorgularına tablo bazında yanıt veren sahte bağlantı."""

    def __init__(self, freshness, gaps=(), tests=(), anomalies=()):
        self.freshness, self.gaps, self.tests, self.anomalies = freshness, list(gaps), list(tests), list(anomalies)

    def execute(self, sql, params=()):
        if sql == alerts.FRESHNESS_SQL:
            rows = self.freshness
        elif sql == alerts.MISSING_GAPS_SQL:
            rows = self.gaps
        elif "ops.dbt_test_results" in sql:
            rows = self.tests
        elif "mart_price_anomalies" in sql:
            cols = ["instrument_code", "anomaly_type", "severity", "n_days", "last_date", "max_abs_metric"]
            return SimpleNamespace(description=[SimpleNamespace(name=c) for c in cols],
                                   fetchall=lambda: [tuple(a[c] for c in cols) for a in self.anomalies])
        else:  # pragma: no cover
            raise AssertionError(sql)
        return SimpleNamespace(fetchall=lambda: list(rows))

    def rollback(self):
        pass


def test_collect_problems_combines_all_checks():
    ref = date(2026, 11, 27)
    fresh = [("fx", pair, ref) for pair in ("USDTRY", "EURTRY")]
    fresh += [("stocks", s, ref) for s in alerts.expected_instruments()["stocks"] if s != "THYAO"]
    fresh += [("stocks", "THYAO", date(2026, 11, 20)), ("gold", "XAUUSD_OZ", ref), ("gold", "XAUTRY_GRAM", ref)]
    tests = [("u1", "not_null_adj_close", "not_null", "fact_daily_prices", "adj_close", "error", "fail", 3, None, 0.1),
             ("u2", "accepted_range_rsi", "accepted_range", "mart_indicators", "rsi_14", "warn", "warn", 1, None, 0.1)]
    gaps = [("fx", "EURTRY", 2, date(2026, 11, 24), date(2026, 11, 25))]
    anomalies = [{"instrument_code": "ASELS", "anomaly_type": "return_spike", "severity": "error", "n_days": 1,
                  "last_date": date(2026, 11, 26), "max_abs_metric": 0.4},
                 {"instrument_code": "SISE", "anomaly_type": "stale_price", "severity": "warn", "n_days": 5,
                  "last_date": date(2026, 11, 26), "max_abs_metric": 5}]
    conn = CollectConn(fresh, gaps, tests, anomalies)
    problems = alerts.collect_problems(conn, ref, NOW, max_lag=2)
    got = {(p.severity, p.kind, p.subject) for p in problems}
    assert got == {("error", "stale_data", "stocks/THYAO"),
                   ("error", "dbt_test", "fact_daily_prices.adj_close.not_null_adj_close"),
                   ("warn", "missing_days", "fx/EURTRY"),
                   ("warn", "price_anomaly", "ASELS/return_spike")}
    assert next(p for p in problems if p.kind == "missing_days").detail == "2 gün eksik (2026-11-24..2026-11-25)"


def test_collect_problems_without_schema():
    import psycopg

    class NoTables:
        def execute(self, sql, params=()):
            raise psycopg.errors.UndefinedTable("yok")

        def rollback(self):
            pass

    problems = alerts.collect_problems(NoTables(), date(2026, 11, 27), NOW)
    assert [(p.kind, p.subject) for p in problems] == [("stale_data", "raw")]


# ------------------------------------------------------------------------------------------ Airflow geri çağırma


def airflow_context(exc=ValueError("dbt build başarısız (çıkış 1)")):
    ti = SimpleNamespace(dag_id="bvt_daily_prices", task_id="dbt_build_marts", try_number=3, max_tries=2,
                         log_url="http://localhost:8080/log?x=1")
    return {"task_instance": ti, "run_id": "scheduled__2026-11-26T16:30:00+00:00", "exception": exc}


def test_failure_alert_from_context():
    alert = alerts.failure_alert(airflow_context())
    assert alert.kind == "task_failed" and alert.severity == "error"
    assert alert.title == "BVT görev hatası: bvt_daily_prices.dbt_build_marts"
    text = alert.text()
    assert "deneme 3/3" in text and "ValueError: dbt build başarısız" in text and "localhost:8080/log" in text
    assert alert.dedup_key == "task_failed:bvt_daily_prices:dbt_build_marts:scheduled__2026-11-26T16:30:00+00:00"


def test_failure_alert_tolerates_sparse_context():
    alert = alerts.failure_alert({})
    assert "?" in alert.title and "bilinmiyor" in alert.text()


def test_task_failure_callback_sends_and_never_raises(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notifiers_from_env", lambda: [StubNotifier("telegram")])
    monkeypatch.setattr(alerts, "send_with_dedup", lambda alert, notifiers, now, hours: sent.append(alert))
    alerts.task_failure_callback(airflow_context())
    assert sent and sent[0].kind == "task_failed"

    def explode(*a, **k):
        raise RuntimeError("beklenmeyen")

    monkeypatch.setattr(alerts, "send_with_dedup", explode)
    alerts.task_failure_callback(airflow_context())  # hata yutulur


# ------------------------------------------------------------------------------------------------------- CLI


def test_cli_test_without_channels(monkeypatch):
    monkeypatch.setattr(alerts, "notifiers_from_env", lambda: [])
    assert alerts.main(["--test"]) == 1


def test_cli_test_sends(monkeypatch, capsys):
    notifier = StubNotifier("telegram")
    monkeypatch.setattr(alerts, "notifiers_from_env", lambda: [notifier])
    assert alerts.main(["--test"]) == 0
    assert notifier.sent[0].kind == "test" and "ok" in capsys.readouterr().out


def test_cli_force_disables_cooldown(monkeypatch):
    seen = {}

    def fake_check(end, dry_run=False, cooldown=None):
        seen.update(end=end, dry_run=dry_run, cooldown=cooldown)
        return {"status": "sent"}

    monkeypatch.setattr(alerts, "check_and_alert", fake_check)
    assert alerts.main(["--end", "2026-11-27", "--force"]) == 0
    assert seen == {"end": date(2026, 11, 27), "dry_run": False, "cooldown": 0}


def test_alerts_sql_init_script_exists():
    from bvt.db import SQL_INIT_DIR

    sql = (SQL_INIT_DIR / "008_ops_alerts.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ops.alerts" in sql and "dedup_key" in sql
