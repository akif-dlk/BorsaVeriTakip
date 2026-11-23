"""dbt'yi proje ayarlarıyla çalıştıran ince sarmalayıcı (Airflow görevi ve elle çalıştırma için).

dbt, Airflow ile bağımlılık çakışması yaşamasın diye ayrı bir sanal ortamda kuruludur (imajda `/opt/dbt-venv`,
yerelde `pip install -r requirements-dbt.txt`). Bu modül dbt'yi Python API'si yerine alt süreç olarak çağırır:
yalnızca standart kütüphaneyi kullanır, hangi ortamda kurulu olduğuna bakmaz.

- Çalıştırılabilir dosya: `DBT_BIN` > PATH'teki `dbt`.
- Proje ve profil dizini: `BVT_DBT_DIR` > repo kökündeki `dbt/`. `profiles.yml` bağlantıyı `POSTGRES_*`
  ortam değişkenlerinden okur; `.env` burada yüklenir (dbt kendisi `.env` okumaz).
- dbt çıktısı satır satır loglanır; başarısızlıkta son satırlarla birlikte `DbtError` fırlatılır (Airflow görevi
  böylece düşer).

Kullanım:
    python -m bvt.dbt_runner debug                              # bağlantı kontrolü
    python -m bvt.dbt_runner build --select source:raw staging  # staging görünümleri + testleri
    python -m bvt.dbt_runner build --select instrument_metadata marts  # star schema (dim_*, fact_*) + testleri
    python -m bvt.dbt_runner                                    # varsayılan: staging build
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

log = logging.getLogger("bvt.dbt_runner")

DEFAULT_DBT_DIR = Path(__file__).resolve().parents[2] / "dbt"
# Staging katmanı: raw kaynak testleri + stg_* görünümleri ve testleri
STAGING_SELECT = ("source:raw", "staging")
# Marts katmanı: enstrüman tanımları (seed) + star schema (dim_instrument, dim_date, fact_daily_prices) ve testleri.
# Staging'e bağımlıdır ama onu yeniden kurmaz; günlük DAG'de staging görevinden sonra çalışır.
MARTS_SELECT = ("instrument_metadata", "marts")
TAIL_LINES = 30
# Airflow görevinin execution_timeout'u (30 dk) dolmadan dbt kendi hatasıyla düşsün
BUILD_TIMEOUT_S = 20 * 60


class DbtError(RuntimeError):
    """dbt sıfırdan farklı çıkış koduyla bitti."""

    def __init__(self, args: list[str], returncode: int, tail: list[str]):
        self.dbt_args, self.returncode, self.tail = args, returncode, tail
        detail = "\n".join(tail)
        super().__init__(f"dbt {' '.join(args)} başarısız (çıkış kodu {returncode}):\n{detail}")


def dbt_dir() -> Path:
    return Path(os.getenv("BVT_DBT_DIR") or DEFAULT_DBT_DIR)


def dbt_executable() -> str:
    explicit = os.getenv("DBT_BIN")
    if explicit:
        return explicit
    found = shutil.which("dbt")
    if found:
        return found
    raise FileNotFoundError(
        "dbt bulunamadı. Kurulum: pip install -r requirements-dbt.txt (ayrı bir sanal ortam önerilir) "
        "veya DBT_BIN ile çalıştırılabilir dosyanın yolunu verin."
    )


def build_command(args: list[str], project_dir: Path | None = None) -> list[str]:
    """`dbt <args> --project-dir <dir> --profiles-dir <dir>`; kullanıcı dizin verdiyse üzerine yazılmaz."""
    directory = str(project_dir or dbt_dir())
    cmd = [dbt_executable(), *args]
    if "--project-dir" not in args:
        cmd += ["--project-dir", directory]
    if "--profiles-dir" not in args:
        cmd += ["--profiles-dir", directory]
    return cmd


def dbt_env() -> dict[str, str]:
    """Alt süreç ortamı: .env değerleri + UTF-8 modu (Windows'ta dbt, Türkçe karakterli YAML'ı yerel kod
    sayfasıyla okumaya çalışıp düşer)."""
    from bvt import config  # noqa: F401 - içe aktarıldığında .env'i ortam değişkenlerine yükler

    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_dbt(args: list[str], timeout: float | None = None) -> list[str]:
    """dbt'yi çalıştırır, çıktısını loglar, tüm çıktı satırlarını döner. Hata durumunda `DbtError`."""
    cmd = build_command(args)
    log.info("dbt çalıştırılıyor: %s", " ".join(args), extra={"dbt_args": args})
    lines: list[str] = []
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    timed_out = threading.Event()
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", env=dbt_env(), cwd=str(dbt_dir())) as proc:
        # Çıktı okunurken (satır gelmese bile) süre aşılırsa süreci öldüren bekçi; okuma döngüsü EOF ile biter.
        watchdog = None
        if timeout is not None:
            watchdog = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
            watchdog.daemon = True
            watchdog.start()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    log.info("dbt | %s", line)
                    lines.append(line)
                    tail.append(line)
            returncode = proc.wait()
        finally:
            if watchdog is not None:
                watchdog.cancel()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout, output="\n".join(tail))
    if returncode != 0:
        raise DbtError(args, returncode, list(tail))
    return lines


def summarize(lines: list[str]) -> dict:
    """dbt'nin son özet satırını ("Done. PASS=.. WARN=.. ERROR=.. SKIP=.. TOTAL=..") sözlüğe çevirir."""
    for line in reversed(lines):
        if "PASS=" in line and "TOTAL=" in line:
            counts = {}
            for token in line.replace("Done.", "").split():
                key, sep, value = token.partition("=")
                if sep and value.isdigit():
                    counts[key.lower()] = int(value)
            return counts
    return {}


def build_staging() -> dict:
    """Airflow görevinin çağırdığı fonksiyon: raw kaynak testleri + staging görünümleri. Özet XCom'a yazılır."""
    lines = run_dbt(["build", "--select", *STAGING_SELECT], timeout=BUILD_TIMEOUT_S)
    return {"select": " ".join(STAGING_SELECT), **summarize(lines)}


def build_marts() -> dict:
    """Airflow görevinin çağırdığı fonksiyon: seed + star schema tabloları ve testleri. Özet XCom'a yazılır."""
    lines = run_dbt(["build", "--select", *MARTS_SELECT], timeout=BUILD_TIMEOUT_S)
    return {"select": " ".join(MARTS_SELECT), **summarize(lines)}


def main(argv: list[str] | None = None) -> int:
    from bvt.logging_setup import setup_logging

    setup_logging()
    args = list(sys.argv[1:] if argv is None else argv) or ["build", "--select", *STAGING_SELECT]
    try:
        lines = run_dbt(args)
    except (DbtError, OSError) as exc:  # OSError: dbt bulunamadı / çalıştırılamadı
        log.error("%s", exc)
        return 1
    summary = summarize(lines)
    if summary:
        log.info("dbt özeti: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
