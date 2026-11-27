"""Hata alarmı: DAG görevi başarısız olursa ya da veri bayatsa Telegram ve/veya e-posta bildirimi gönderir.

İki giriş noktası vardır:

1. **Görev hatası** (`task_failure_callback`): DAG'in `default_args.on_failure_callback`'idir. Airflow onu yalnızca
   görev son denemesinde de başarısız olduğunda çağırır (yeniden denemelerde değil); mesajda DAG, görev, çalışma,
   deneme sayısı, hata ve log bağlantısı yer alır. Geri çağırma hiçbir koşulda hata fırlatmaz.
2. **Sağlık kontrolü** (`check_and_alert`): DAG'in son görevi `check_alerts` ve CLI. Veritabanından şunları toplar:
   - `stale_data` (error): enstrümanın son verisi referans günden `BVT_STALE_AFTER_DAYS` (varsayılan 2) iş
     gününden eski ya da hiç verisi yok. Hafta sonları sayılmaz; tek günlük tatil eşiğe takılmaz, uzun bayramlar
     takılabilir (bilerek: o zaman bir kez uyarmak, haftalarca sessiz kalmaktan iyidir).
   - `dbt_test` (error): son `REPORT_WINDOW_HOURS` saatteki dbt çağrılarında `fail` / `error` veren testler.
   - `missing_days` (warn): son `ALERT_GAP_DAYS` gündeki gerçek veri eksikleri (`ops.data_gaps`, kind='missing').
   - `price_anomaly` (warn): son `ALERT_ANOMALY_DAYS` gündeki error düzeyli fiyat anormallikleri.
   Sorun yoksa bildirim gönderilmez.

Kanallar ortam değişkenlerinden kurulur; boş bırakılan kanal devre dışıdır, hiçbiri yoksa alarm yalnızca loglanır:
    Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    E-posta : SMTP_HOST, SMTP_PORT (587; 465 = SSL), SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_FROM, ALERT_EMAIL_TO
              (virgülle birden çok alıcı), SMTP_STARTTLS (varsayılan true)

Tekrarı önleme: her gönderim `ops.alerts`'a yazılır; aynı sorun kümesi (`dedup_key`) `BVT_ALERT_COOLDOWN_HOURS`
(varsayılan 12) saat içinde yeniden gönderilmez. Günlük DAG için bu, süren bir soruna günde bir hatırlatma demektir.
Veritabanına ulaşılamazsa tekrar önleme atlanır ve alarm yine gönderilir (fail-open).

Gizli bilgiler (bot token'ı, SMTP şifresi) loglara ve `ops.alerts`'a yazılmaz; hata mesajlarından ayıklanır.

Kullanım:
    python -m bvt.alerts                 # sağlık kontrolü, sorun varsa bildir
    python -m bvt.alerts --dry-run       # mesajı yazdır, gönderme
    python -m bvt.alerts --test          # yapılandırılmış kanallara deneme mesajı
    python -m bvt.alerts --end 2026-11-27 --force   # referans gün, bekleme süresini yok say
"""
from __future__ import annotations

import argparse
import hashlib
import html
import logging
import os
import smtplib
import ssl
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Protocol

from bvt import config

log = logging.getLogger("bvt.alerts")

SEVERITIES = ("error", "warn", "info")
DEFAULT_STALE_AFTER_DAYS = 2
DEFAULT_COOLDOWN_HOURS = 12
ALERT_GAP_DAYS = 7
ALERT_ANOMALY_DAYS = 3
MAX_LISTED_PROBLEMS = 25
TELEGRAM_MAX_CHARS = 4096
TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT_S = 15


# ------------------------------------------------------------------------------------------------ alarm modeli


@dataclass(frozen=True, order=True)
class Problem:
    severity: str   # error | warn
    kind: str       # stale_data | dbt_test | missing_days | price_anomaly | task_failed
    subject: str    # ör. "stocks/THYAO", "fact_daily_prices.not_null_adj_close"
    detail: str

    def line(self) -> str:
        icon = "🔴" if self.severity == "error" else "🟡"
        return f"{icon} [{self.kind}] {self.subject}: {self.detail}"


@dataclass(frozen=True)
class Alert:
    kind: str                       # health_check | task_failed | test
    title: str
    problems: tuple[Problem, ...] = ()
    footer: str = ""
    dedup_key: str = field(default="")

    @property
    def severity(self) -> str:
        if not self.problems:
            return "info"
        return "error" if any(p.severity == "error" for p in self.problems) else "warn"

    def _listed(self) -> tuple[list[Problem], int]:
        ordered = sorted(self.problems, key=lambda p: (SEVERITIES.index(p.severity), p.kind, p.subject))
        return ordered[:MAX_LISTED_PROBLEMS], max(0, len(ordered) - MAX_LISTED_PROBLEMS)

    def text(self) -> str:
        listed, rest = self._listed()
        lines = [self.title, ""] + [p.line() for p in listed]
        if rest:
            lines.append(f"… ve {rest} sorun daha")
        if self.footer:
            lines += ["", self.footer]
        return "\n".join(lines).strip()

    def html(self) -> str:
        """Telegram `parse_mode=HTML` gövdesi; tüm dinamik metin kaçışlıdır."""
        listed, rest = self._listed()
        lines = [f"<b>{html.escape(self.title)}</b>", ""]
        lines += [html.escape(p.line()) for p in listed]
        if rest:
            lines.append(f"… ve {rest} sorun daha")
        if self.footer:
            lines += ["", f"<i>{html.escape(self.footer)}</i>"]
        return "\n".join(lines).strip()


def problems_key(prefix: str, problems: Iterable[Problem]) -> str:
    """Sorun kümesinin kararlı anahtarı: ayrıntı (ör. gecikme gün sayısı) değişse de aynı sorun aynı anahtarı verir."""
    parts = sorted(f"{p.kind}|{p.subject}" for p in problems)
    return f"{prefix}:" + hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------------------------------- kanallar


class Notifier(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class AlertDeliveryError(RuntimeError):
    """Kanal mesajı kabul etmedi (yeniden denemeyle düzelmeyecek hata: yanlış token, sohbet yok...)."""


class _TransientDeliveryError(ConnectionError):
    """Yeniden denenebilir teslim hatası (HTTP 429 / 5xx)."""


def redact(text: str, secrets: Iterable[str | None]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TelegramNotifier:
    """Bot API `sendMessage` (HTML). 429/5xx ve ağ hataları üstel beklemeyle yeniden denenir, 4xx denenmez."""

    name = "telegram"

    def __init__(self, token: str, chat_id: str, session=None, attempts: int = 3,
                 sleep: Callable[[float], None] | None = None):
        self.token, self.chat_id = token, chat_id
        self.session, self.attempts, self.sleep = session, attempts, sleep

    def _post(self, payload: dict) -> None:
        import requests

        session = self.session or requests
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        response = session.post(url, json=payload, timeout=HTTP_TIMEOUT_S)
        if response.status_code == 429 or response.status_code >= 500:
            raise _TransientDeliveryError(f"Telegram HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or not body.get("ok", False):
            raise AlertDeliveryError(f"Telegram HTTP {response.status_code}: {body.get('description', 'yanıt yok')}")

    def send(self, alert: Alert) -> None:
        import requests

        from bvt.retry import retry_call

        payload = {"chat_id": self.chat_id, "text": _truncate(alert.html(), TELEGRAM_MAX_CHARS),
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            retry_call(self._post, payload, attempts=self.attempts, base_delay=2.0, max_delay=20.0,
                       exceptions=(_TransientDeliveryError, requests.ConnectionError, requests.Timeout),
                       sleep=self.sleep)
        except (requests.RequestException, ConnectionError) as exc:
            # requests hataları URL'yi (dolayısıyla token'ı) içerebilir: zincir koparılır, mesaj ayıklanır.
            raise AlertDeliveryError(redact(f"{type(exc).__name__}: {exc}", [self.token])) from None


class EmailNotifier:
    """SMTP (STARTTLS ya da 465'te SSL). Düz metin gövde; konu alarm başlığıdır."""

    name = "email"

    def __init__(self, host: str, port: int, sender: str, recipients: list[str], user: str | None = None,
                 password: str | None = None, starttls: bool = True, smtp_factory=None):
        self.host, self.port, self.sender, self.recipients = host, port, sender, recipients
        self.user, self.password, self.starttls = user, password, starttls
        self.smtp_factory = smtp_factory

    def message(self, alert: Alert) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = alert.title
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(alert.text())
        return msg

    def send(self, alert: Alert) -> None:
        use_ssl = self.port == 465
        factory = self.smtp_factory or (smtplib.SMTP_SSL if use_ssl else smtplib.SMTP)
        try:
            with factory(self.host, self.port, timeout=HTTP_TIMEOUT_S) as smtp:
                if self.starttls and not use_ssl:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password or "")
                smtp.send_message(self.message(alert))
        except (smtplib.SMTPException, OSError) as exc:
            raise AlertDeliveryError(redact(f"{type(exc).__name__}: {exc}", [self.password])) from None


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "evet")


def notifiers_from_env(env: Mapping[str, str] | None = None) -> list[Notifier]:
    """Ortam değişkenlerinden yapılandırılmış kanallar. Eksik yapılandırılmış kanal uyarıyla atlanır."""
    env = os.environ if env is None else env
    get = lambda name: (env.get(name) or "").strip()  # noqa: E731
    notifiers: list[Notifier] = []

    token, chat_id = get("TELEGRAM_BOT_TOKEN"), get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        notifiers.append(TelegramNotifier(token, chat_id))
    elif token or chat_id:
        log.warning("Telegram kanalı eksik yapılandırılmış: TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID birlikte gerekli")

    host, recipients = get("SMTP_HOST"), [r.strip() for r in get("ALERT_EMAIL_TO").split(",") if r.strip()]
    if host and recipients:
        port_raw = get("SMTP_PORT") or "587"
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(f"SMTP_PORT tam sayı olmalı: {port_raw!r}") from exc
        user = get("SMTP_USER") or None
        sender = get("ALERT_EMAIL_FROM") or user or recipients[0]
        notifiers.append(EmailNotifier(host, port, sender, recipients, user, get("SMTP_PASSWORD") or None,
                                       starttls=_flag(env.get("SMTP_STARTTLS"), True)))
    elif host or recipients:
        log.warning("E-posta kanalı eksik yapılandırılmış: SMTP_HOST ve ALERT_EMAIL_TO birlikte gerekli")
    return notifiers


def dispatch(alert: Alert, notifiers: list[Notifier]) -> dict[str, str]:
    """Alarmı tüm kanallara gönderir; bir kanalın hatası diğerlerini engellemez. {kanal: 'ok' | hata} döner."""
    results: dict[str, str] = {}
    for notifier in notifiers:
        try:
            notifier.send(alert)
            results[notifier.name] = "ok"
        except Exception as exc:  # noqa: BLE001 - alarm gönderimi hiçbir kanal yüzünden düşmemeli
            results[notifier.name] = _truncate(str(exc), 300)
            log.error("Alarm %s kanalına gönderilemedi: %s", notifier.name, results[notifier.name],
                      extra={"channel": notifier.name, "alert_kind": alert.kind})
    if not notifiers:
        log.warning("Alarm kanalı yapılandırılmamış (TELEGRAM_* / SMTP_*); alarm yalnızca loglandı:\n%s",
                    alert.text())
    return results


# -------------------------------------------------------------------------------------------- sağlık kontrolü

FRESHNESS_SQL = """
SELECT 'fx' AS source, pair AS instrument, max(rate_date) AS last_date
  FROM raw.fx_rates WHERE source = 'yfinance' GROUP BY pair
UNION ALL
SELECT 'stocks', symbol, max(price_date) FROM raw.stock_prices WHERE source = 'yfinance' GROUP BY symbol
UNION ALL
SELECT 'gold', instrument, max(price_date) FROM raw.gold_prices GROUP BY instrument
"""

MISSING_GAPS_SQL = """
SELECT source, instrument, count(*) AS n_days, min(gap_date) AS first_date, max(gap_date) AS last_date
  FROM ops.data_gaps
 WHERE kind = 'missing' AND gap_date >= %s
 GROUP BY source, instrument
 ORDER BY source, instrument
"""


def expected_instruments() -> dict[str, list[str]]:
    """İzlenmesi gereken enstrümanlar (config): hiç verisi olmayan enstrüman da bayat sayılır."""
    return {"fx": list(config.FX_PAIRS), "stocks": config.bist_symbols(), "gold": list(config.GOLD_INSTRUMENTS)}


def business_days_after(last: date, ref: date) -> int:
    """`last`'ten sonra `ref` dahil kaç hafta içi gün var (veri gecikmesi, iş günü cinsinden)."""
    if last >= ref:
        return 0
    days, day = 0, last + timedelta(days=1)
    while day <= ref:
        days += day.weekday() < 5
        day += timedelta(days=1)
    return days


def stale_after_days(value: int | None = None) -> int:
    if value is None:
        raw = os.getenv("BVT_STALE_AFTER_DAYS") or str(DEFAULT_STALE_AFTER_DAYS)
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"BVT_STALE_AFTER_DAYS tam sayı olmalı: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"BVT_STALE_AFTER_DAYS negatif olamaz: {value}")
    return value


def stale_problems(last_dates: Mapping[tuple[str, str], date], ref: date, max_lag: int,
                   expected: Mapping[str, list[str]] | None = None) -> list[Problem]:
    """(kaynak, enstrüman) → son veri günü eşlemesinden bayat veri sorunları."""
    expected = expected_instruments() if expected is None else expected
    keys = set(last_dates) | {(source, inst) for source, insts in expected.items() for inst in insts}
    problems = []
    for source, instrument in sorted(keys):
        last = last_dates.get((source, instrument))
        subject = f"{source}/{instrument}"
        if last is None:
            problems.append(Problem("error", "stale_data", subject, "hiç veri yok"))
            continue
        lag = business_days_after(last, ref)
        if lag > max_lag:
            problems.append(Problem("error", "stale_data", subject,
                                    f"son veri {last.isoformat()} ({lag} iş günü gecikme, eşik {max_lag})"))
    return problems


def _query(conn, sql: str, params=()) -> list[tuple] | None:
    """Sorgu; tablo henüz yoksa (şema/marts kurulmadı) None."""
    import psycopg

    try:
        return conn.execute(sql, params).fetchall()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None


def collect_problems(conn, ref: date, now: datetime, max_lag: int | None = None) -> list[Problem]:
    """Veritabanından tüm sağlık sorunlarını toplar (bkz. modül belgesi)."""
    from bvt import dbt_report

    problems: list[Problem] = []
    rows = _query(conn, FRESHNESS_SQL)
    if rows is None:
        problems.append(Problem("error", "stale_data", "raw", "raw tabloları yok (şema kurulmamış)"))
    else:
        last_dates = {(source, inst): last for source, inst, last in rows if last is not None}
        problems += stale_problems(last_dates, ref, stale_after_days(max_lag))

    try:
        tests = dbt_report.fetch_latest(conn, now - timedelta(hours=dbt_report.REPORT_WINDOW_HOURS))
    except Exception as exc:  # noqa: BLE001 - tablo yoksa ya da sorgu bozuksa kontrolün geri kalanı sürer
        conn.rollback()
        log.warning("dbt test sonuçları okunamadı: %s", exc)
        tests = []
    for t in tests:
        if t.status in ("fail", "error"):
            where = ".".join(x for x in (t.model, t.column_name) if x) or "-"
            rows_txt = f", {t.failures} satır" if t.failures else ""
            problems.append(Problem("error", "dbt_test", f"{where}.{t.test_name}"[:200], f"{t.status}{rows_txt}"))

    gaps = _query(conn, MISSING_GAPS_SQL, (ref - timedelta(days=ALERT_GAP_DAYS),))
    for source, instrument, n_days, first, last in gaps or []:
        span = first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}"
        problems.append(Problem("warn", "missing_days", f"{source}/{instrument}", f"{n_days} gün eksik ({span})"))

    anomalies = dbt_report.fetch_anomalies(conn, ref - timedelta(days=ALERT_ANOMALY_DAYS)) or []
    for a in anomalies:
        if a["severity"] == "error":
            problems.append(Problem("warn", "price_anomaly", f"{a['instrument_code']}/{a['anomaly_type']}",
                                    f"{a['n_days']} gün, son {a['last_date']}"))
    return problems


def health_alert(problems: list[Problem], ref: date) -> Alert:
    errors = sum(p.severity == "error" for p in problems)
    warns = len(problems) - errors
    title = f"BVT veri alarmı {ref.isoformat()}: {errors} hata, {warns} uyarı"
    footer = "Rapor: python -m bvt.dbt_report · Onarım: python -m bvt.backfill --repair-gaps"
    return Alert("health_check", title, tuple(problems), footer, problems_key("health", problems))


# ------------------------------------------------------------------------------------------ tekrar önleme / kayıt

RECENT_SQL = "SELECT 1 FROM ops.alerts WHERE dedup_key = %s AND sent_at >= %s AND cardinality(channels) > 0 LIMIT 1"
INSERT_SQL = """
INSERT INTO ops.alerts (dedup_key, kind, severity, title, body, channels, errors)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def cooldown_hours(value: int | None = None) -> int:
    if value is not None:
        return value
    raw = os.getenv("BVT_ALERT_COOLDOWN_HOURS") or str(DEFAULT_COOLDOWN_HOURS)
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"BVT_ALERT_COOLDOWN_HOURS tam sayı olmalı: {raw!r}") from exc


def recently_sent(conn, key: str, now: datetime, hours: int) -> bool:
    if hours <= 0:
        return False
    rows = _query(conn, RECENT_SQL, (key, now - timedelta(hours=hours)))
    return bool(rows)


def record_alert(conn, alert: Alert, results: dict[str, str]) -> None:
    ok = [name for name, status in results.items() if status == "ok"]
    errors = "; ".join(f"{name}: {status}" for name, status in results.items() if status != "ok") or None
    conn.execute(INSERT_SQL, (alert.dedup_key, alert.kind, alert.severity, alert.title, alert.text(), ok, errors))
    conn.commit()


def send_with_dedup(alert: Alert, notifiers: list[Notifier], now: datetime, hours: int, conn=None) -> dict:
    """Bekleme süresi içinde aynı alarm gönderildiyse atlar; değilse gönderir ve kaydeder.

    `conn` None ise kendi bağlantısını açmayı dener; veritabanı erişilemezse tekrar önleme ve kayıt atlanır."""
    from contextlib import nullcontext

    if conn is None:
        try:
            from bvt.db import connect

            ctx = connect(attempts=1)
            conn = ctx.__enter__()
        except Exception as exc:  # noqa: BLE001 - veritabanı çökmüş olabilir; alarm yine gitmeli
            log.warning("ops.alerts'a erişilemedi, tekrar önleme atlanıyor: %s", exc)
            ctx, conn = nullcontext(), None
    else:
        ctx = nullcontext()
    try:
        if conn is not None and recently_sent(conn, alert.dedup_key, now, hours):
            log.info("Alarm son %d saatte gönderilmiş, atlandı: %s", hours, alert.title,
                     extra={"dedup_key": alert.dedup_key})
            return {"status": "suppressed", "channels": {}}
        results = dispatch(alert, notifiers)
        if conn is not None and notifiers:
            try:
                record_alert(conn, alert, results)
            except Exception as exc:  # noqa: BLE001 - kayıt hatası gönderilmiş alarmı geri almaz
                log.warning("Alarm ops.alerts'a kaydedilemedi: %s", exc)
        delivered = any(status == "ok" for status in results.values())
        return {"status": "sent" if delivered else ("logged" if not notifiers else "failed"), "channels": results}
    finally:
        ctx.__exit__(None, None, None)


def check_and_alert(run_end: datetime | date | None = None, notifiers: list[Notifier] | None = None,
                    now: datetime | None = None, cooldown: int | None = None, dry_run: bool = False) -> dict:
    """Airflow `check_alerts` görevi ve CLI: sağlık sorunlarını toplar, varsa bildirir. Özet XCom'a döner.

    Bildirim gönderilemese de görev düşmez (sonuç `status='failed'` ile döner ve loglanır): alarm hattının arızası
    veri hattını kırmızıya boyamamalı; bu durum sağlık panelinde `ops.alerts.errors` ile görünür."""
    from bvt.db import connect
    from bvt.pipeline import MARKET_TZ, market_date

    now = now or datetime.now(timezone.utc)
    ref = market_date(run_end if run_end is not None else now.astimezone(MARKET_TZ))
    with connect() as conn:
        problems = collect_problems(conn, ref, now)
        summary = {"ref_date": ref.isoformat(), "problems": len(problems),
                   "errors": sum(p.severity == "error" for p in problems),
                   "warnings": sum(p.severity == "warn" for p in problems)}
        if not problems:
            log.info("Sağlık kontrolü temiz (%s)", ref, extra=summary)
            return {**summary, "status": "ok"}
        alert = health_alert(problems, ref)
        if dry_run:
            print(alert.text())
            return {**summary, "status": "dry_run"}
        notifiers = notifiers_from_env() if notifiers is None else notifiers
        result = send_with_dedup(alert, notifiers, now, cooldown_hours(cooldown), conn)
    log.warning("Sağlık kontrolü: %d sorun, alarm %s", len(problems), result["status"], extra=summary)
    return {**summary, **result}


# ------------------------------------------------------------------------------------------- Airflow geri çağırma


def failure_alert(context: Mapping) -> Alert:
    """Airflow görev bağlamından görev hatası alarmı."""
    ti = context.get("task_instance") or context.get("ti")
    dag_id = getattr(ti, "dag_id", None) or getattr(context.get("dag"), "dag_id", "?")
    task_id = getattr(ti, "task_id", "?")
    run_id = context.get("run_id") or getattr(context.get("dag_run"), "run_id", "?")
    exc = context.get("exception")
    error = _truncate(" ".join(f"{type(exc).__name__}: {exc}".split()), 500) if exc else "bilinmiyor"
    try_number = getattr(ti, "try_number", None)
    max_tries = getattr(ti, "max_tries", None)
    tries = f"{try_number}/{max_tries + 1}" if isinstance(try_number, int) and isinstance(max_tries, int) else "-"
    problem = Problem("error", "task_failed", f"{dag_id}.{task_id}", f"deneme {tries}, hata: {error}")
    log_url = getattr(ti, "log_url", None)
    footer = f"Çalışma: {run_id}" + (f" · Log: {log_url}" if log_url else "")
    return Alert("task_failed", f"BVT görev hatası: {dag_id}.{task_id}", (problem,), footer,
                 f"task_failed:{dag_id}:{task_id}:{run_id}")


def task_failure_callback(context: Mapping) -> None:
    """Airflow `on_failure_callback`: görev son denemede de düştüğünde bildirir. Asla hata fırlatmaz."""
    try:
        alert = failure_alert(context)
        log.error("%s", alert.text())
        send_with_dedup(alert, notifiers_from_env(), datetime.now(timezone.utc), cooldown_hours())
    except Exception:  # noqa: BLE001 - geri çağırmadaki hata Airflow'da görünmez ve başka bir şeyi düzeltmez
        log.exception("Görev hatası alarmı gönderilemedi")


# ---------------------------------------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    from bvt.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Veri sağlık kontrolü ve Telegram/e-posta alarmı.")
    parser.add_argument("--end", type=date.fromisoformat, help="referans gün YYYY-MM-DD (varsayılan: bugün)")
    parser.add_argument("--dry-run", action="store_true", help="mesajı yazdır, gönderme")
    parser.add_argument("--force", action="store_true", help="bekleme süresini (tekrar önleme) yok say")
    parser.add_argument("--test", action="store_true", help="yapılandırılmış kanallara deneme mesajı gönder")
    args = parser.parse_args(argv)
    setup_logging()

    if args.test:
        notifiers = notifiers_from_env()
        if not notifiers:
            log.error("Kanal yapılandırılmamış: .env'de TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID ya da SMTP_* doldurun")
            return 1
        alert = Alert("test", "BVT alarm denemesi", footer="Bu mesajı görüyorsanız kanal çalışıyor.")
        results = dispatch(alert, notifiers)
        print(results)
        return 0 if all(status == "ok" for status in results.values()) else 1

    import psycopg

    try:
        result = check_and_alert(args.end, dry_run=args.dry_run, cooldown=0 if args.force else None)
    except (psycopg.Error, ValueError) as exc:
        log.error("%s", exc)
        return 1
    print(result)
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
