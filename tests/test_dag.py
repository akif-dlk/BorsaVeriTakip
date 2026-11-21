"""DAG tanımının yapısal testi.

Airflow yerelde kurulu değildir (Windows'ta desteklenmez, imaj içinde çalışır). Bu yüzden `airflow` ve `pendulum`
modülleri küçük sahte nesnelerle değiştirilip DAG dosyası içe aktarılır; görevler, zamanlama ve görev
fonksiyonunun `bvt.pipeline`'a doğru argümanlarla gittiği doğrulanır. Gerçek Airflow ayrıştırması için:
    docker compose run --rm airflow-scheduler airflow dags list-import-errors
"""
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bvt import pipeline

DAG_FILE = Path(__file__).resolve().parents[1] / "airflow" / "dags" / "bvt_daily_prices.py"


class FakeDAG:
    current = None

    def __init__(self, dag_id, **kwargs):
        self.dag_id, self.kwargs, self.tasks = dag_id, kwargs, []

    def __enter__(self):
        FakeDAG.current = self
        return self

    def __exit__(self, *exc):
        FakeDAG.current = None
        return False


class FakePythonOperator:
    def __init__(self, task_id, python_callable, op_kwargs=None, **kwargs):
        self.task_id, self.python_callable, self.op_kwargs = task_id, python_callable, op_kwargs or {}
        self.kwargs, self.upstream = kwargs, []
        assert FakeDAG.current is not None, "görev DAG bağlamı dışında tanımlandı"
        FakeDAG.current.tasks.append(self)

    def __rrshift__(self, other):  # [a, b] >> self
        self.upstream.extend(other if isinstance(other, list) else [other])
        return self


@pytest.fixture
def dag_module(monkeypatch):
    airflow = types.ModuleType("airflow")
    airflow.DAG = FakeDAG
    operators = types.ModuleType("airflow.operators")
    python_ops = types.ModuleType("airflow.operators.python")
    python_ops.PythonOperator = FakePythonOperator
    trigger_rule = types.ModuleType("airflow.utils.trigger_rule")
    trigger_rule.TriggerRule = types.SimpleNamespace(ALL_DONE="all_done")
    pendulum = types.ModuleType("pendulum")
    pendulum.datetime = lambda *a, tz=None: ("pendulum", a, tz)
    for name, mod in {"airflow": airflow, "airflow.operators": operators, "airflow.operators.python": python_ops,
                      "airflow.utils": types.ModuleType("airflow.utils"), "airflow.utils.trigger_rule": trigger_rule,
                      "pendulum": pendulum}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("bvt_daily_prices", DAG_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dag_definition(dag_module):
    dag = dag_module.dag
    assert dag.dag_id == "bvt_daily_prices"
    assert dag.kwargs["schedule"] == "30 19 * * 1-5"
    assert dag.kwargs["catchup"] is False
    assert dag.kwargs["max_active_runs"] == 1
    assert dag.kwargs["start_date"][2] == "Europe/Istanbul"
    args = dag.kwargs["default_args"]
    assert args["retries"] >= 1 and args["retry_exponential_backoff"] is True
    assert args["max_retry_delay"] >= args["retry_delay"]


def test_one_independent_task_per_source(dag_module):
    fetch = [t for t in dag_module.dag.tasks if t.task_id.startswith("fetch_")]
    assert [t.task_id for t in fetch] == [f"fetch_{s}" for s in pipeline.SOURCES]
    assert [t.op_kwargs["source"] for t in fetch] == list(pipeline.SOURCES)
    assert all(not t.upstream for t in fetch)  # paralel, birbirine bağımlı değil


def test_gap_check_runs_after_all_fetches_even_on_failure(dag_module):
    gap = next(t for t in dag_module.dag.tasks if t.task_id == "check_gaps")
    assert [t.task_id for t in gap.upstream] == [f"fetch_{s}" for s in pipeline.SOURCES]
    assert gap.kwargs["trigger_rule"] == "all_done"


def test_gap_check_callable(dag_module, monkeypatch):
    seen = {}
    monkeypatch.setattr(pipeline, "check_gaps", lambda run_end: seen.setdefault("end", run_end) and {"ok": 1})
    gap = next(t for t in dag_module.dag.tasks if t.task_id == "check_gaps")
    end = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc)
    assert gap.python_callable(data_interval_end=end, ti=object()) == {"ok": 1}
    assert seen["end"] == end


def test_task_callable_uses_data_interval_end(dag_module, monkeypatch):
    seen = {}

    def fake_run_daily(source, run_end, trigger):
        seen.update(source=source, run_end=run_end, trigger=trigger)
        return {"rows": 1}

    monkeypatch.setattr(pipeline, "run_daily", fake_run_daily)
    end = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc)
    task = dag_module.dag.tasks[0]
    # Airflow bağlamı anahtar kelime argümanları olarak geçirir; fazlası **context ile yutulur.
    assert task.python_callable(**task.op_kwargs, data_interval_end=end, ti=object(), ds="2026-11-18") == {"rows": 1}
    assert seen == {"source": "fx", "run_end": end, "trigger": "airflow"}


def test_dbt_staging_runs_after_fetches_even_on_failure(dag_module):
    task = next(t for t in dag_module.dag.tasks if t.task_id == "dbt_build_staging")
    assert [t.task_id for t in task.upstream] == [f"fetch_{s}" for s in pipeline.SOURCES]
    assert task.kwargs["trigger_rule"] == "all_done"
    assert task.kwargs["retries"] == 0


def test_dbt_staging_callable(dag_module, monkeypatch):
    from bvt import dbt_runner

    monkeypatch.setattr(dbt_runner, "build_staging", lambda: {"pass": 3})
    task = next(t for t in dag_module.dag.tasks if t.task_id == "dbt_build_staging")
    assert task.python_callable(ti=object(), ds="2026-11-18") == {"pass": 3}
