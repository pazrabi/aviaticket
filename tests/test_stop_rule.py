"""Тесты правила остановки живого поиска (src/avia/stop_rule.py)."""
from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from avia.stop_rule import StopParams, clear_cache, decide, stable_seconds_for


@pytest.fixture(autouse=True)
def _reset_cache():
    """Сбрасывает кеш правил остановки перед каждым тестом."""
    clear_cache()
    yield
    clear_cache()


def test_decide_all_reasons_and_order():
    """Проверка всех причин остановки и их строгого приоритета:

    1. captcha
    2. done (wait_for_seen и n_offers >= min_offers)
    3. stable (best_min, n_offers >= min_offers и elapsed - t_last_improve >= stable_seconds)
    4. quiet (network_quiet и best_min)
    5. job_done (job_state in ('done', 'failed'))
    6. cap (elapsed >= cap_seconds)
    7. None (пока рано)
    """
    params = StopParams(stable_seconds=8.0, min_offers=1, cap_seconds=45.0)

    # 1. captcha всегда на первом месте, даже если все остальные условия выполнены
    st_captcha = {
        "captcha": True,
        "wait_for_seen": True,
        "n_offers": 5,
        "best_min": 1000,
        "t_last_improve": 0.0,
        "elapsed": 50.0,
        "network_quiet": True,
        "job_state": "done",
    }
    assert decide(st_captcha, params) == "captcha"

    # 2. done при wait_for_seen и наличии хотя бы min_offers
    st_done = {
        "captcha": False,
        "wait_for_seen": True,
        "n_offers": 1,
        "best_min": 2000,
        "t_last_improve": 0.0,
        "elapsed": 10.0,
        "network_quiet": True,
        "job_state": "running",
    }
    assert decide(st_done, params) == "done"

    # Если wait_for_seen истинно, но n_offers < min_offers — done не возвращается
    st_done_no_offers = {
        "captcha": False,
        "wait_for_seen": True,
        "n_offers": 0,
        "best_min": None,
        "t_last_improve": None,
        "elapsed": 5.0,
        "job_state": "running",
    }
    assert decide(st_done_no_offers, params) is None

    # 3. stable: best_min есть, n_offers >= min_offers и elapsed - t_last_improve >= stable_seconds
    # Проверка: stable ровно на границе (elapsed - t_last_improve == 8.0)
    st_stable_exact = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 2,
        "best_min": 3000,
        "t_last_improve": 2.0,
        "elapsed": 10.0,  # 10.0 - 2.0 = 8.0 (ровно stable_seconds)
        "network_quiet": True,
        "job_state": "running",
    }
    assert decide(st_stable_exact, params) == "stable"

    # Чуть раньше границы (7.9 сек) — stable ещё нет, но сработает quiet, если есть
    st_stable_before = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 2,
        "best_min": 3000,
        "t_last_improve": 2.0,
        "elapsed": 9.9,  # 9.9 - 2.0 = 7.9 < 8.0
        "network_quiet": False,
        "job_state": "running",
    }
    assert decide(st_stable_before, params) is None

    # Больше границы (8.1 сек) -> stable
    st_stable_after = dict(st_stable_before, elapsed=10.1)
    assert decide(st_stable_after, params) == "stable"

    # Приоритет done над stable: если wait_for_seen=True, возвращается done
    st_done_over_stable = dict(st_stable_exact, wait_for_seen=True)
    assert decide(st_done_over_stable, params) == "done"

    # 4. quiet: network_quiet и best_min есть
    st_quiet = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 1,
        "best_min": 4000,
        "t_last_improve": 2.0,
        "elapsed": 4.0,  # 4.0 - 2.0 = 2.0 < 8.0 (не stable)
        "network_quiet": True,
        "job_state": "running",
    }
    assert decide(st_quiet, params) == "quiet"

    # network_quiet без best_min не останавливает
    st_quiet_no_min = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 0,
        "best_min": None,
        "t_last_improve": None,
        "elapsed": 4.0,
        "network_quiet": True,
        "job_state": "running",
    }
    assert decide(st_quiet_no_min, params) is None

    # 5. job_done: state in ('done', 'failed') без других причин
    st_job_done = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 0,
        "best_min": None,
        "t_last_improve": None,
        "elapsed": 12.0,
        "network_quiet": False,
        "job_state": "done",
    }
    assert decide(st_job_done, params) == "job_done"

    st_job_failed = dict(st_job_done, job_state="failed")
    assert decide(st_job_failed, params) == "job_done"

    # Приоритет quiet над job_done: если quiet и best_min есть, quiet возвращается раньше
    st_quiet_over_job_done = dict(st_quiet, job_state="done")
    assert decide(st_quiet_over_job_done, params) == "quiet"

    # 6. cap: elapsed >= cap_seconds
    st_cap = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 0,
        "best_min": None,
        "t_last_improve": None,
        "elapsed": 45.0,
        "network_quiet": False,
        "job_state": "running",
    }
    assert decide(st_cap, params) == "cap"

    # Приоритет job_done над cap:
    st_job_done_over_cap = dict(st_cap, job_state="done")
    assert decide(st_job_done_over_cap, params) == "job_done"

    # 7. None пока рано
    st_early = {
        "captcha": False,
        "wait_for_seen": False,
        "n_offers": 1,
        "best_min": 5000,
        "t_last_improve": 3.0,
        "elapsed": 5.0,  # 5.0 - 3.0 = 2.0 < 8.0, < cap_seconds
        "network_quiet": False,
        "job_state": "running",
    }
    assert decide(st_early, params) is None


def test_decide_supports_object_state():
    """decide корректно извлекает поля из объекта с атрибутами."""
    params = StopParams(stable_seconds=5.0, min_offers=1, cap_seconds=30.0)
    obj = SimpleNamespace(
        captcha=False,
        wait_for_seen=False,
        n_offers=2,
        best_min=4500.0,
        t_last_improve=1.0,
        elapsed=6.0,
        network_quiet=False,
        job_state="running",
    )
    assert decide(obj, params) == "stable"


def test_stable_seconds_for(tmp_path):
    """Тест stable_seconds_for:

    - нет файла -> значение по умолчанию
    - файл с значением для сайта -> значение из файла
    - битый файл -> значение по умолчанию
    - кеш файла по mtime
    """
    settings = SimpleNamespace(storage_dir=tmp_path, stop_stable_seconds=8.0)

    # 1. Нет файла -> 8.0
    assert stable_seconds_for("aviasales", settings) == 8.0

    # 2. Файл с настройкой для сайта
    rules_path = tmp_path / "stop_rules.json"
    rules_path.write_text(json.dumps({"aviasales": {"stable_seconds": 6.5}}), encoding="utf-8")

    assert stable_seconds_for("aviasales", settings) == 6.5
    # Другой сайт в файле отсутствует -> по умолчанию
    assert stable_seconds_for("tutu", settings) == 8.0

    # 3. Обновление файла (кеш по mtime)
    time.sleep(0.05)
    rules_path.write_text(json.dumps({"aviasales": {"stable_seconds": 5.0}}), encoding="utf-8")
    # Должен считать обновлённое значение
    assert stable_seconds_for("aviasales", settings) == 5.0

    # 4. Битый JSON -> значение по умолчанию без падения
    time.sleep(0.05)
    rules_path.write_text("invalid json {{{", encoding="utf-8")
    assert stable_seconds_for("aviasales", settings) == 8.0

    # 5. Если settings не задан -> 8.0
    clear_cache()
    assert stable_seconds_for("any_site", None) == 8.0
