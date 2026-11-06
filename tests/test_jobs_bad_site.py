"""Тест фоллбэка заданий хаба на старый fetch_page при пустой выдаче (PLAN_SPEED шаг 10)."""
from __future__ import annotations

import asyncio
import time
import pytest

from avia.collector import Snapshot
from avia.config import Settings
import avia.droidhub as droidhub
from avia.droidhub import (
    JOBS_BAD_FIRST_SECONDS,
    JOBS_BAD_SITE_SECONDS,
    JOBS_BAD_WINDOW_SECONDS,
    _jobs_bad_sites,
    _jobs_bad_strikes,
    _reset_pool,
    fetch_page_snapshot_any,
    reset_jobs_supported,
)


@pytest.fixture(autouse=True)
def clean_jobs_state():
    _reset_pool()
    reset_jobs_supported()
    yield
    _reset_pool()
    reset_jobs_supported()


@pytest.mark.asyncio
async def test_jobs_bad_site_fallback_and_cooldown(monkeypatch):
    """(Г) Если fetch_job завершился stop_reason=='job_done' без json_payloads при capture_json:

    1. Вызывается _fetch_device_snapshot (старый fetch_page);
    2. Первый удар: пауза JOBS_BAD_FIRST_SECONDS (300 с), сайт попадает в _jobs_bad_sites и _jobs_bad_strikes;
    3. Во время паузы следующий вызов для этого сайта идёт сразу через fetch_page;
    4. Для другого сайта — вызов по-прежнему идёт через fetch_job;
    5. Второй удар в окне (1800 с): пауза JOBS_BAD_SITE_SECONDS (21600 с);
    6. Удар вне окна считается первым (пауза 300 с);
    7. Успешный ответ задания после паузы сбрасывает счётчик ударов;
    8. reset_jobs_supported очищает оба словаря.
    """
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )

    job_calls: list[tuple[str, str, dict]] = []
    snapshot_calls: list[tuple[str, str, dict]] = []

    async def mock_fetch_device_job(settings, dev, url, **kwargs):
        job_calls.append((dev, url, kwargs))
        # Возвращаем job_done без json_payloads при capture_json
        snap = Snapshot(url=url, text="job_empty", json_payloads=[], blocked=False)
        info = {"stop_reason": "job_done", "elapsed": 1.5}
        return snap, True, info

    async def mock_fetch_device_snapshot(settings, dev, url, **kwargs):
        snapshot_calls.append((dev, url, kwargs))
        snap = Snapshot(
            url=url,
            text="snapshot_ok",
            json_payloads=[("https://api.test/results", '{"offers": [1]}')],
            blocked=False,
        )
        return snap, True

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_fetch_device_job)
    monkeypatch.setattr(droidhub, "_fetch_device_snapshot", mock_fetch_device_snapshot)

    async def dummy_eval(snap, state):
        return None

    # 1. Первый вызов для bad_site: идёт в fetch_job, тот отдаёт job_done без json_payloads
    snap1, fin1 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://bad-site.test/search",
        site_name="bad_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )

    # Должен быть вызван fetch_job, а затем фоллбэк на _fetch_device_snapshot
    assert len(job_calls) == 1
    assert job_calls[0][0] == "dev1"
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][0] == "dev1"
    assert snap1.text == "snapshot_ok"

    # Первый удар: сайт в _jobs_bad_sites с паузой 300 с (JOBS_BAD_FIRST_SECONDS)
    assert "bad_site" in _jobs_bad_sites
    now = time.monotonic()
    assert abs(_jobs_bad_sites["bad_site"] - (now + JOBS_BAD_FIRST_SECONDS)) < 5.0
    assert len(_jobs_bad_strikes["bad_site"]) == 1

    # 2. Второй вызов для bad_site во время паузы: сайт в _jobs_bad_sites -> идёт сразу через fetch_page
    snap2, fin2 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://bad-site.test/search",
        site_name="bad_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )

    # fetch_job НЕ вызывался, snapshot_calls увеличился
    assert len(job_calls) == 1
    assert len(snapshot_calls) == 2
    assert snap2.text == "snapshot_ok"

    # 3. Вызов для другого сайта (good_site): сайт не в _jobs_bad_sites -> идёт через fetch_job
    snap3, fin3 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://good-site.test/search",
        site_name="good_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )

    # Для good_site был вызван fetch_job (job_calls увеличился)
    assert len(job_calls) == 2
    assert job_calls[1][1] == "https://good-site.test/search"

    # 4. Второй удар в окне (первая пауза 300 с истекла, но 1800 с окно ещё активно)
    _jobs_bad_sites["bad_site"] = time.monotonic() - 1.0

    snap4, fin4 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://bad-site.test/search",
        site_name="bad_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )

    # Срок истёк -> fetch_job снова используется, второй удар -> пауза 21600 с (JOBS_BAD_SITE_SECONDS)
    assert len(job_calls) == 3
    assert job_calls[2][1] == "https://bad-site.test/search"
    assert len(_jobs_bad_strikes["bad_site"]) == 2
    now = time.monotonic()
    assert abs(_jobs_bad_sites["bad_site"] - (now + JOBS_BAD_SITE_SECONDS)) < 5.0

    # 5. Удар вне окна считается первым
    # Имитируем, что предыдущий удар был давно (более JOBS_BAD_WINDOW_SECONDS=1800 назад)
    _jobs_bad_sites["bad_site"] = time.monotonic() - 1.0
    _jobs_bad_strikes["bad_site"] = [time.monotonic() - (JOBS_BAD_WINDOW_SECONDS + 10.0)]

    snap5, fin5 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://bad-site.test/search",
        site_name="bad_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )

    assert len(job_calls) == 4
    # Старый удар отброшен, теперь только 1 текущий удар
    assert len(_jobs_bad_strikes["bad_site"]) == 1
    now = time.monotonic()
    assert abs(_jobs_bad_sites["bad_site"] - (now + JOBS_BAD_FIRST_SECONDS)) < 5.0

    # 6. Успешный ответ после паузы сбрасывает счётчик ударов
    _jobs_bad_sites["bad_site"] = time.monotonic() - 1.0

    async def mock_fetch_device_job_ok(settings, dev, url, **kwargs):
        job_calls.append((dev, url, kwargs))
        snap = Snapshot(
            url=url,
            text="job_ok",
            json_payloads=[("https://api.test/results", '{"offers": [1]}')],
            blocked=False,
        )
        info = {"stop_reason": "job_done", "elapsed": 1.0}
        return snap, True, info

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_fetch_device_job_ok)

    snap6, fin6 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://bad-site.test/search",
        site_name="bad_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )

    assert len(job_calls) == 5
    assert snap6.text == "job_ok"
    assert snap6.json_payloads
    # Счётчик ударов очищен
    assert not _jobs_bad_strikes.get("bad_site")

    # Следующий сбой после успешного ответа снова считается первым ударом (300 с)
    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_fetch_device_job)
    snap7, fin7 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://bad-site.test/search",
        site_name="bad_site",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
    )
    assert len(job_calls) == 6
    assert len(_jobs_bad_strikes["bad_site"]) == 1
    now = time.monotonic()
    assert abs(_jobs_bad_sites["bad_site"] - (now + JOBS_BAD_FIRST_SECONDS)) < 5.0

    # 7. Очистка обоих словарей между тестами
    reset_jobs_supported()
    assert len(_jobs_bad_sites) == 0
    assert len(_jobs_bad_strikes) == 0
