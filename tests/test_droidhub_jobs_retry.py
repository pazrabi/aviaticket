"""Тесты повтора пустого задания хаба на другом телефоне перед fetch_page."""
from __future__ import annotations

import asyncio
import sqlite3
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
from avia.metrics import MetricsStore


@pytest.fixture(autouse=True)
def clean_jobs_state():
    _reset_pool()
    reset_jobs_supported()
    yield
    _reset_pool()
    reset_jobs_supported()


@pytest.mark.asyncio
async def test_retry_on_second_phone_when_first_empty(monkeypatch, tmp_path):
    """(а) Первый телефон отдаёт пустое задание, второй нормальное:

    - результат со второго телефона;
    - паузы для сайта нет (_jobs_bad_sites пуст);
    - fetch_page (_fetch_device_snapshot) не вызывался;
    - удар сброшен успехом;
    - в attempts записана попытка с исходом empty для dev1.
    """
    db_path = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(db_path)
    metrics._ensure_schema()

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
        metrics=metrics,
    )

    job_calls: list[tuple[str, str]] = []
    snapshot_calls: list[tuple[str, str]] = []

    async def mock_fetch_device_job(settings, dev, url, **kwargs):
        job_calls.append((dev, url))
        if dev == "dev1":
            # Первое устройство возвращает job_done без json_payloads
            snap = Snapshot(url=url, text="empty_1", json_payloads=[], blocked=False)
            info = {"stop_reason": "job_done", "elapsed": 1.0}
            return snap, True, info
        # Второе устройство возвращает нормальный результат с json_payloads
        snap = Snapshot(
            url=url,
            text="ok_2",
            json_payloads=[("https://api.test/results", '{"offers": [1]}')],
            blocked=False,
        )
        info = {"stop_reason": "job_done", "elapsed": 1.5}
        return snap, True, info

    async def mock_fetch_device_snapshot(settings, dev, url, **kwargs):
        snapshot_calls.append((dev, url))
        snap = Snapshot(url=url, text="snapshot_fallback", json_payloads=[], blocked=False)
        return snap, True

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_fetch_device_job)
    monkeypatch.setattr(droidhub, "_fetch_device_snapshot", mock_fetch_device_snapshot)

    async def dummy_eval(snap, state):
        return None

    stats: dict = {}
    snap, finished = await fetch_page_snapshot_any(
        settings,
        ["dev1", "dev2"],
        "https://site.test/search",
        site_name="site_test",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
        stats=stats,
    )

    # 1. Результат со второго телефона
    assert snap.text == "ok_2"
    assert len(snap.json_payloads) == 1
    assert len(job_calls) == 2
    assert job_calls[0][0] == "dev1"
    assert job_calls[1][0] == "dev2"

    # 2. fetch_page не вызывался
    assert len(snapshot_calls) == 0

    # 3. Паузы для сайта нет
    assert "site_test" not in _jobs_bad_sites

    # 4. Успех второго телефона сбросил удары сайта
    assert not _jobs_bad_strikes.get("site_test")

    # 5. Stats зафиксировали попытки
    assert stats["device"] == "dev2"
    assert stats["attempts"] == 2
    assert set(stats["tried"]) == {"dev1", "dev2"}

    # 6. В attempts записана неудачная попытка для dev1 с outcome='empty'
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attempts WHERE site = 'site_test'").fetchall()
        assert len(rows) == 1
        assert rows[0]["device"] == "dev1"
        assert rows[0]["outcome"] == "empty"
        assert rows[0]["role"] == "primary"


@pytest.mark.asyncio
async def test_both_phones_empty_triggers_fallback_and_pause(monkeypatch):
    """(б) Оба телефона отдают пустое задание:

    - срабатывает fetch_page (_fetch_device_snapshot) на втором телефоне;
    - пауза JOBS_BAD_FIRST_SECONDS (300 с) ставится;
    - удары не накручиваются дважды за один вызов: ровно 1 удар в _jobs_bad_strikes.
    """
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )

    job_calls: list[tuple[str, str]] = []
    snapshot_calls: list[tuple[str, str]] = []

    async def mock_fetch_device_job(settings, dev, url, **kwargs):
        job_calls.append((dev, url))
        snap = Snapshot(url=url, text=f"empty_{dev}", json_payloads=[], blocked=False)
        info = {"stop_reason": "job_done", "elapsed": 1.0}
        return snap, True, info

    async def mock_fetch_device_snapshot(settings, dev, url, **kwargs):
        snapshot_calls.append((dev, url))
        snap = Snapshot(
            url=url,
            text=f"snapshot_{dev}",
            json_payloads=[("https://api.test/results", '{"offers": [1]}')],
            blocked=False,
        )
        return snap, True

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_fetch_device_job)
    monkeypatch.setattr(droidhub, "_fetch_device_snapshot", mock_fetch_device_snapshot)

    async def dummy_eval(snap, state):
        return None

    stats: dict = {}
    snap, finished = await fetch_page_snapshot_any(
        settings,
        ["dev1", "dev2"],
        "https://site.test/search",
        site_name="site_test",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
        stats=stats,
    )

    # Оба телефона опробованы в job
    assert len(job_calls) == 2
    assert job_calls[0][0] == "dev1"
    assert job_calls[1][0] == "dev2"

    # Сработал fetch_page на dev2
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][0] == "dev2"
    assert snap.text == "snapshot_dev2"

    # Пауза установлена (300 с для первого удара)
    assert "site_test" in _jobs_bad_sites
    now = time.monotonic()
    assert abs(_jobs_bad_sites["site_test"] - (now + JOBS_BAD_FIRST_SECONDS)) < 5.0

    # Удары не накручены дважды: ровно 1 удар в окне
    assert len(_jobs_bad_strikes["site_test"]) == 1


@pytest.mark.asyncio
async def test_single_candidate_empty_triggers_immediate_fallback(monkeypatch):
    """(в) Единственный телефон-кандидат пуст:

    - сразу старое поведение (fetch_page на dev1 + пауза);
    - второй попытки нет, так как кандидатов больше нет.
    """
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )

    job_calls: list[tuple[str, str]] = []
    snapshot_calls: list[tuple[str, str]] = []

    async def mock_fetch_device_job(settings, dev, url, **kwargs):
        job_calls.append((dev, url))
        snap = Snapshot(url=url, text="empty_single", json_payloads=[], blocked=False)
        info = {"stop_reason": "job_done", "elapsed": 1.0}
        return snap, True, info

    async def mock_fetch_device_snapshot(settings, dev, url, **kwargs):
        snapshot_calls.append((dev, url))
        snap = Snapshot(
            url=url,
            text="snapshot_single",
            json_payloads=[("https://api.test/results", '{"offers": [1]}')],
            blocked=False,
        )
        return snap, True

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_fetch_device_job)
    monkeypatch.setattr(droidhub, "_fetch_device_snapshot", mock_fetch_device_snapshot)

    async def dummy_eval(snap, state):
        return None

    stats: dict = {}
    snap, finished = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://site.test/search",
        site_name="site_test",
        capture_json=["api.test"],
        evaluate=dummy_eval,
        priority=0,
        stats=stats,
    )

    # 1 вызов fetch_job на dev1, 1 вызов fetch_page на dev1
    assert len(job_calls) == 1
    assert job_calls[0][0] == "dev1"
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][0] == "dev1"
    assert snap.text == "snapshot_single"

    # Пауза установлена
    assert "site_test" in _jobs_bad_sites
    assert len(_jobs_bad_strikes["site_test"]) == 1


@pytest.mark.asyncio
async def test_success_resets_strikes_and_strike_progression(monkeypatch):
    """(г) Успех сбрасывает удары, а повторный удар в окне даёт паузу 6 ч."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )

    async def dummy_eval(snap, state):
        return None

    # 1. Первый вызов: оба пустые -> удар 1, пауза 300 с
    async def mock_job_empty(settings, dev, url, **kwargs):
        snap = Snapshot(url=url, text="empty", json_payloads=[], blocked=False)
        return snap, True, {"stop_reason": "job_done"}

    async def mock_snapshot(settings, dev, url, **kwargs):
        return Snapshot(url=url, text="snap", json_payloads=[("api", "{}")]), True

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_job_empty)
    monkeypatch.setattr(droidhub, "_fetch_device_snapshot", mock_snapshot)

    await fetch_page_snapshot_any(
        settings, ["dev1", "dev2"], "https://site.test/search",
        site_name="site_test", capture_json=["api"], evaluate=dummy_eval, priority=0,
    )
    assert len(_jobs_bad_strikes["site_test"]) == 1
    now = time.monotonic()
    assert abs(_jobs_bad_sites["site_test"] - (now + JOBS_BAD_FIRST_SECONDS)) < 5.0

    # 2. Имитируем истечение паузы 300 с (окно 1800 с ещё активно)
    _jobs_bad_sites["site_test"] = time.monotonic() - 1.0

    # Вызов успешен (dev1 пустой, dev2 успешен) -> сброс ударов
    async def mock_job_first_empty_second_ok(settings, dev, url, **kwargs):
        if dev == "dev1":
            return Snapshot(url=url, text="empty", json_payloads=[]), True, {"stop_reason": "job_done"}
        return Snapshot(url=url, text="ok", json_payloads=[("api", "{}")]), True, {"stop_reason": "job_done"}

    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_job_first_empty_second_ok)

    snap, _ = await fetch_page_snapshot_any(
        settings, ["dev1", "dev2"], "https://site.test/search",
        site_name="site_test", capture_json=["api"], evaluate=dummy_eval, priority=0,
    )
    assert snap.text == "ok"
    # Успех сбросил удары!
    assert not _jobs_bad_strikes.get("site_test")
    assert "site_test" not in _jobs_bad_sites or _jobs_bad_sites["site_test"] <= time.monotonic()

    # 3. Следующий сбой после сброса считается первым ударом (пауза 300 с, а не 21600 с)
    monkeypatch.setattr(droidhub, "_fetch_device_job", mock_job_empty)

    await fetch_page_snapshot_any(
        settings, ["dev1", "dev2"], "https://site.test/search",
        site_name="site_test", capture_json=["api"], evaluate=dummy_eval, priority=0,
    )
    assert len(_jobs_bad_strikes["site_test"]) == 1
    now = time.monotonic()
    assert abs(_jobs_bad_sites["site_test"] - (now + JOBS_BAD_FIRST_SECONDS)) < 5.0

    # 4. Теперь второй удар в окне (пауза 300 с истекла) без сброса -> пауза 21600 с (JOBS_BAD_SITE_SECONDS)
    _jobs_bad_sites["site_test"] = time.monotonic() - 1.0

    await fetch_page_snapshot_any(
        settings, ["dev1", "dev2"], "https://site.test/search",
        site_name="site_test", capture_json=["api"], evaluate=dummy_eval, priority=0,
    )
    assert len(_jobs_bad_strikes["site_test"]) == 2
    now = time.monotonic()
    assert abs(_jobs_bad_sites["site_test"] - (now + JOBS_BAD_SITE_SECONDS)) < 5.0
