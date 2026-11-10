"""Тесты вытеснения фоновой загрузки живым поиском."""
from __future__ import annotations

import asyncio
from datetime import date
import sqlite3
import time
from typing import Any

import httpx
import pytest

from avia.config import Settings, SiteConfig
from avia.dispatcher import check_site_fuse
from avia.droidhub import (
    ActiveLoad,
    DroidhubError,
    PreemptedError,
    _active_loads,
    _fetch_device_job,
    _last_preempt,
    _reset_pool,
    acquire,
    fetch_page_snapshot_any,
    release,
    set_clock,
)
from avia.health import collect_health
from avia.metrics import MetricsStore
from avia.models import Query
from avia.pipeline import search


_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _mock_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "avia.droidhub.httpx.AsyncClient",
        lambda **kwargs: _ORIG_ASYNC_CLIENT(
            transport=transport,
            **{k: v for k, v in kwargs.items() if k != "transport"},
        ),
    )


class FakeClock:
    def __init__(self, current: float = 1000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, dt: float) -> None:
        self.current += dt


@pytest.fixture(autouse=True)
def clean_pool():
    _reset_pool()
    yield
    _reset_pool()


# ---------------------------------------------------------------------------
# (а) Человек вытесняет фон
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_preempts_background(monkeypatch):
    """(а) Человек вытесняет фон: фон занимает единственный телефон,
    человек acquire -> фон получает PreemptedError, человек получает телефон.
    """
    s = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")

    bg_started = asyncio.Event()

    async def handler(request: httpx.Request):
        bg_started.set()
        await asyncio.sleep(10.0)
        return httpx.Response(200, json={"url": str(request.url), "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, handler)

    bg_exc = None

    async def bg_load():
        nonlocal bg_exc
        try:
            await fetch_page_snapshot_any(s, ["dev1"], "https://example.com", priority=2)
        except Exception as e:
            bg_exc = e
            raise

    t_bg = asyncio.create_task(bg_load())
    await bg_started.wait()

    assert "dev1" in _active_loads
    assert _active_loads["dev1"].priority == 2

    human_dev = await acquire(["dev1"], priority=0)
    assert human_dev == "dev1"

    with pytest.raises(PreemptedError):
        await t_bg
    assert isinstance(bg_exc, PreemptedError)

    await release(human_dev)


# ---------------------------------------------------------------------------
# (б) Вытеснение не чаще раза в 20 с на телефон
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preempt_cooldown_20_seconds():
    """(б) Вытеснение не чаще раза в 20 с на телефон."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # 1. Занимаем dev1 фоновой задачей в t = 100
    d1 = await acquire(["dev1"], priority=2)
    cancelled1 = False

    def cancel_cb1():
        nonlocal cancelled1
        cancelled1 = True
        asyncio.create_task(release("dev1"))

    load1 = ActiveLoad(priority=2, cancel=cancel_cb1, started_at=clock(), preempted=False)
    _active_loads["dev1"] = load1

    # Первый человек вытесняет
    human1_dev = await acquire(["dev1"], priority=0)
    assert human1_dev == "dev1"
    assert load1.preempted is True
    assert cancelled1 is True
    assert _last_preempt["dev1"] == 100.0
    await release(human1_dev)

    # 2. dev1 снова занят фоном через 5 секунд (t = 105)
    clock.advance(5.0)
    d2 = await acquire(["dev1"], priority=2)
    cancelled2 = False

    def cancel_cb2():
        nonlocal cancelled2
        cancelled2 = True
        asyncio.create_task(release("dev1"))

    load2 = ActiveLoad(priority=2, cancel=cancel_cb2, started_at=clock(), preempted=False)
    _active_loads["dev1"] = load2

    # Человек 2 пытается вытеснить в t = 105 (прошло только 5 с, кулдаун 20 с)
    human2_task = asyncio.create_task(acquire(["dev1"], priority=0))
    await asyncio.sleep(0.01)

    # Вытеснения НЕ произошло: load2.preempted всё ещё False, cancelled2 всё ещё False
    assert load2.preempted is False
    assert cancelled2 is False
    assert not human2_task.done()

    # Фон 2 завершается сам
    _active_loads.pop("dev1", None)
    await release(d2)

    # Человек 2 получил телефон штатно после освобождения
    human2_dev = await human2_task
    assert human2_dev == "dev1"
    await release(human2_dev)

    # 3. dev1 снова занят фоном в t = 125 (> 20 с с момента вытеснения на t = 100)
    clock.advance(20.0)
    d3 = await acquire(["dev1"], priority=2)
    cancelled3 = False

    def cancel_cb3():
        nonlocal cancelled3
        cancelled3 = True
        asyncio.create_task(release("dev1"))

    load3 = ActiveLoad(priority=2, cancel=cancel_cb3, started_at=clock(), preempted=False)
    _active_loads["dev1"] = load3

    # Человек 3 пытается вытеснить в t = 125 (прошло 25 с > 20 с)
    human3_dev = await acquire(["dev1"], priority=0)
    assert human3_dev == "dev1"
    assert load3.preempted is True
    assert cancelled3 is True
    assert _last_preempt["dev1"] == 125.0
    await release(human3_dev)


# ---------------------------------------------------------------------------
# (в) PREEMPT=0 — человек ждёт
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preempt_disabled_env(monkeypatch):
    """(в) PREEMPT=0 в окружении — человек ждёт."""
    monkeypatch.setenv("PREEMPT", "0")

    d1 = await acquire(["dev1"], priority=2)
    load = ActiveLoad(priority=2, cancel=lambda: None, started_at=time.monotonic(), preempted=False)
    _active_loads["dev1"] = load

    human_task = asyncio.create_task(acquire(["dev1"], priority=0))
    await asyncio.sleep(0.02)

    assert load.preempted is False
    assert not human_task.done()

    _active_loads.pop("dev1", None)
    await release(d1)

    human_dev = await human_task
    assert human_dev == "dev1"
    await release(human_dev)


@pytest.mark.asyncio
async def test_preempt_disabled_settings():
    """(в) Settings.preempt=False — человек ждёт."""
    s = Settings(sites=[], preempt=False)

    d1 = await acquire(["dev1"], priority=2)
    load = ActiveLoad(priority=2, cancel=lambda: None, started_at=time.monotonic(), preempted=False)
    _active_loads["dev1"] = load

    human_task = asyncio.create_task(acquire(["dev1"], priority=0, settings=s))
    await asyncio.sleep(0.02)

    assert load.preempted is False
    assert not human_task.done()

    _active_loads.pop("dev1", None)
    await release(d1)

    human_dev = await human_task
    assert human_dev == "dev1"
    await release(human_dev)


# ---------------------------------------------------------------------------
# (г) Вытесненный фон не пишет ошибку в health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preempted_background_no_health_error(tmp_path, monkeypatch):
    """(г) Вытесненный фон не пишет ошибку в health."""
    metrics_path = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(metrics_path)

    site = SiteConfig("test_site", "https://example.com", phone="dev1")
    s = Settings(
        sites=[site],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        storage_dir=tmp_path,
        metrics=metrics,
    )

    bg_started = asyncio.Event()

    async def handler(request: httpx.Request):
        bg_started.set()
        await asyncio.sleep(10.0)
        return httpx.Response(200, json={"url": str(request.url), "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", depart=date(2026, 12, 1))

    # Запускаем фоновый поиск через search (priority=2)
    bg_search_task = asyncio.create_task(search(q, s, priority=2))
    await bg_started.wait()

    assert "dev1" in _active_loads

    # Человек вызывает acquire
    human_dev = await acquire(["dev1"], priority=0, settings=s)
    assert human_dev == "dev1"

    # Фоновый поиск завершается без исключения наружу (пропуск без ошибки)
    res = await bg_search_task
    assert res == []

    await release(human_dev)

    # Проверяем, что в health ничего не записалось
    health_data = collect_health(metrics, since_ts=0)
    assert "test_site" not in health_data

    # Проверяем, что в site_runs нет записей
    recent = metrics.site_recent_outcomes("test_site", 0)
    assert len(recent) == 0

    # Проверяем, что site_fuse не деградировал
    fused, had_captcha = check_site_fuse("test_site", s)
    assert not fused
    assert not had_captcha


# ---------------------------------------------------------------------------
# (д) Человек не вытесняет другого человека (priority 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_does_not_preempt_human():
    """(д) Человек не вытесняет другого человека (priority 0)."""
    d1 = await acquire(["dev1"], priority=0)
    cancelled = False

    def cancel_cb():
        nonlocal cancelled
        cancelled = True
        asyncio.create_task(release("dev1"))

    load = ActiveLoad(priority=0, cancel=cancel_cb, started_at=time.monotonic(), preempted=False)
    _active_loads["dev1"] = load

    # Человек 2 пытается занять dev1
    human2_task = asyncio.create_task(acquire(["dev1"], priority=0))
    await asyncio.sleep(0.02)

    # Человек 1 НЕ вытеснен: load.preempted False, cancelled False, human2_task ждёт
    assert load.preempted is False
    assert cancelled is False
    assert not human2_task.done()

    # Человек 1 завершает работу штатно
    _active_loads.pop("dev1", None)
    await release(d1)

    # Человек 2 получает телефон штатно
    human2_dev = await human2_task
    assert human2_dev == "dev1"
    await release(human2_dev)


# ---------------------------------------------------------------------------
# Дополнительно: вытесняется самая свежая загрузка среди кандидатов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preempt_picks_freshest_load():
    """Среди кандидатов вытесняется самая свежая фоновая загрузка (started_at максимальный)."""
    # dev1 занят в t = 100
    d1 = await acquire(["dev1"], priority=2)
    load1 = ActiveLoad(priority=2, cancel=lambda: None, started_at=100.0, preempted=False)
    _active_loads["dev1"] = load1

    # dev2 занят в t = 200 (более свежая загрузка)
    d2 = await acquire(["dev2"], priority=2)
    cancelled2 = False

    def cancel_cb2():
        nonlocal cancelled2
        cancelled2 = True
        asyncio.create_task(release("dev2"))

    load2 = ActiveLoad(priority=2, cancel=cancel_cb2, started_at=200.0, preempted=False)
    _active_loads["dev2"] = load2

    # Человек может взять любой из dev1, dev2
    human_dev = await acquire(["dev1", "dev2"], priority=0)
    assert human_dev == "dev2"
    assert load2.preempted is True
    assert cancelled2 is True
    assert load1.preempted is False

    await release(d1)
    await release(human_dev)


# ---------------------------------------------------------------------------
# Задание хаба: отмена вызывает _delete_job через asyncio.shield
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_job_deleted_on_cancelled_error(monkeypatch):
    """(3) Если задание хаба (fetch_job) — отмена вызывает _delete_job через asyncio.shield."""
    deleted_urls = []

    s = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")

    job_started = asyncio.Event()

    async def handler(request: httpx.Request):
        url = str(request.url)
        if request.method == "POST" and url.endswith("/fetch_jobs"):
            job_started.set()
            return httpx.Response(200, json={"job_id": "job_to_cancel_123"})
        if request.method == "GET" and "/fetch_jobs/job_to_cancel_123" in url:
            await asyncio.sleep(10.0)
            return httpx.Response(200, json={"state": "running", "responses": [], "next": 0})
        if request.method == "DELETE" and "/fetch_jobs/job_to_cancel_123" in url:
            deleted_urls.append(url)
            return httpx.Response(200, json={"status": "deleted"})
        return httpx.Response(404)

    _mock_async_client(monkeypatch, handler)

    t = asyncio.create_task(
        _fetch_device_job(
            s,
            "dev1",
            "https://example.com",
            evaluate=lambda s, h: None,
        )
    )
    await job_started.wait()
    await asyncio.sleep(0.01)

    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    assert len(deleted_urls) == 1
    assert "job_to_cancel_123" in deleted_urls[0]
