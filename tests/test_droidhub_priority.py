"""Тесты приоритетов очереди телефонов, списка предпочтений, замеров и панели."""
from __future__ import annotations

import asyncio
from datetime import date
import re
import sqlite3
import time

import httpx
import pytest

from avia import admin_panel
from avia.config import Settings, SiteConfig
from avia.droidhub import (
    DroidhubError,
    PreferenceList,
    _reset_pool,
    acquire,
    candidates,
    fetch_page_snapshot,
    fetch_page_snapshot_any,
    phone_enabled,
    release,
)
from avia.guard import SearchGuard
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
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


@pytest.fixture(autouse=True)
def reset_pool_state():
    _reset_pool()
    yield
    _reset_pool()


# ---------------------------------------------------------------------------
# 1. Приоритеты очереди и тяжесть сайта
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_human_overtakes_subscription_and_background():
    """Человек (priority=0) обгоняет подписку (priority=1) и фон (priority=2)."""
    # Занимаем телефон dev1
    d1 = await acquire(["dev1"])
    assert d1 == "dev1"

    order: list[str] = []

    async def waiter(name: str, priority: int, weight: int):
        dev = await acquire(["dev1"], priority=priority, weight=weight)
        order.append(name)
        await release(dev)

    # Запускаем ожидающих в обратном порядке: сначала фон, затем подписка, затем человек
    t_bg = asyncio.create_task(waiter("bg", priority=2, weight=0))
    await asyncio.sleep(0.005)
    t_sub = asyncio.create_task(waiter("sub", priority=1, weight=0))
    await asyncio.sleep(0.005)
    t_human = asyncio.create_task(waiter("human", priority=0, weight=0))
    await asyncio.sleep(0.005)

    # Освобождаем dev1
    await release(d1)
    await asyncio.gather(t_bg, t_sub, t_human)

    # Человек должен получить телефон первым, затем подписка, затем фон
    assert order == ["human", "sub", "bg"]


@pytest.mark.asyncio
async def test_priority_heavy_site_earlier_at_equal_priority():
    """При равном приоритете тяжёлый сайт (больший weight) получает телефон раньше."""
    d1 = await acquire(["dev1"])
    assert d1 == "dev1"

    order: list[str] = []

    async def waiter(name: str, weight: int):
        dev = await acquire(["dev1"], priority=0, weight=weight)
        order.append(name)
        await release(dev)

    # Лёгкий сайт пришёл раньше (weight=10)
    t_light = asyncio.create_task(waiter("light", weight=10))
    await asyncio.sleep(0.005)
    # Тяжёлый сайт пришёл позже (weight=50)
    t_heavy = asyncio.create_task(waiter("heavy", weight=50))
    await asyncio.sleep(0.005)

    await release(d1)
    await asyncio.gather(t_light, t_heavy)

    # Тяжёлый сайт обогнал лёгкий при равном priority=0
    assert order == ["heavy", "light"]


# ---------------------------------------------------------------------------
# 2. Освободившийся телефон не достаётся чужим кандидатам
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_released_device_not_given_to_waiter_with_different_candidates():
    """Освободившийся телефон не достаётся ожидающему с чужим списком кандидатов."""
    # Занимаем оба телефона
    await acquire(["dev1"])
    await acquire(["dev2"])

    received_dev1: list[str] = []
    received_dev2: list[str] = []

    # Ожидающий A хочет только dev2 с высоким приоритетом
    async def waiter_a():
        dev = await acquire(["dev2"], priority=0, weight=100)
        received_dev2.append(dev)
        await release(dev)

    # Ожидающий B хочет только dev1 с низким приоритетом
    async def waiter_b():
        dev = await acquire(["dev1"], priority=2, weight=0)
        received_dev1.append(dev)
        await release(dev)

    t_a = asyncio.create_task(waiter_a())
    t_b = asyncio.create_task(waiter_b())
    await asyncio.sleep(0.005)

    # Освобождаем dev1: несмотря на то что waiter_a имеет приоритет 0, dev1 должен достаться waiter_b
    await release("dev1")
    await asyncio.sleep(0.01)

    assert received_dev1 == ["dev1"]
    assert received_dev2 == []  # waiter_a всё ещё ждёт dev2

    # Теперь освобождаем dev2: waiter_a просыпается
    await release("dev2")
    await asyncio.gather(t_a, t_b)

    assert received_dev2 == ["dev2"]


# ---------------------------------------------------------------------------
# 3. Отмена ожидающего не теряет телефон и не ломает очередь
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_does_not_lose_device_or_break_queue():
    """Отменённая задача убирается из очереди и не теряет освобождённый телефон."""
    await acquire(["dev1"])

    task_b_result: list[str] = []

    async def waiter_a():
        await acquire(["dev1"], priority=0)

    async def waiter_b():
        dev = await acquire(["dev1"], priority=1)
        task_b_result.append(dev)
        await release(dev)

    t_a = asyncio.create_task(waiter_a())
    t_b = asyncio.create_task(waiter_b())
    await asyncio.sleep(0.005)

    # Отменяем более приоритетного waiter_a
    t_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t_a

    # Освобождаем dev1: телефон должен корректно достаться waiter_b
    await release("dev1")
    await t_b

    assert task_b_result == ["dev1"]


# ---------------------------------------------------------------------------
# 4. Список предпочтений
# ---------------------------------------------------------------------------


def test_candidates_and_phone_enabled_preference_list():
    """Список предпочтений корректно парсится, одиночный id и auto работают."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        droidhub_devices=["auto1", "auto2"],
    )

    # Запятая: список предпочтений с сохранением порядка и удалением дубликатов
    site_pref = SiteConfig("tutu", "https://tutu.test", phone=" realme , redmi , realme ")
    cands = candidates(settings, site_pref)
    assert isinstance(cands, PreferenceList)
    assert cands == ["realme", "redmi"]
    assert phone_enabled(settings, site_pref) is True

    # auto
    site_auto = SiteConfig("auto_site", "https://auto.test", phone="auto")
    cands_auto = candidates(settings, site_auto)
    assert not isinstance(cands_auto, PreferenceList)
    assert cands_auto == ["auto1", "auto2"]
    assert phone_enabled(settings, site_auto) is True

    # одиночный телефон
    site_single = SiteConfig("single_site", "https://single.test", phone="realme")
    assert candidates(settings, site_single) == ["realme"]
    assert phone_enabled(settings, site_single) is True

    # пустой / только запятые
    site_empty = SiteConfig("empty_site", "https://empty.test", phone="  ,  ")
    assert candidates(settings, site_empty) == []
    assert phone_enabled(settings, site_empty) is False


@pytest.mark.asyncio
async def test_acquire_preference_list_takes_first_available_in_order():
    """Для списка предпочтений acquire берёт первый свободный по порядку списка, а не round-robin."""
    pref = PreferenceList(["realme", "redmi"])

    # Когда оба свободны, всегда берётся первый (realme)
    d1 = await acquire(pref)
    assert d1 == "realme"
    await release(d1)

    # При следующем вызове снова realme (не redmi, как было бы в round-robin!)
    d2 = await acquire(pref)
    assert d2 == "realme"

    # Теперь realme занят, должен взяться redmi
    d3 = await acquire(pref)
    assert d3 == "redmi"

    await release(d2)
    await release(d3)


# ---------------------------------------------------------------------------
# 5. Замеры stats (device, queue_wait, fetch_seconds, attempts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_page_snapshot_any_stats_recorded_and_on_retry(monkeypatch):
    """stats корректно заполняется, в том числе при повторе на другом телефоне."""
    async def handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        if dev == "dev1":
            await asyncio.sleep(0.02)
            return httpx.Response(502, text="Bad Gateway")
        await asyncio.sleep(0.03)
        return httpx.Response(
            200,
            json={"url": "https://ok.test", "text": "success", "finished": True},
        )

    _mock_async_client(monkeypatch, handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["dev1", "dev2"],
    )

    stats: dict = {}
    snap, finished = await fetch_page_snapshot_any(
        settings, ["dev1", "dev2"], "https://site.test", stats=stats
    )

    assert finished is True
    assert snap.text == "success"
    assert stats["device"] == "dev2"
    assert stats["attempts"] == 2
    assert stats["queue_wait"] >= 0.0
    assert stats["fetch_seconds"] >= 0.02


@pytest.mark.asyncio
async def test_pipeline_siteresult_receives_stats_and_on_droidhub_error(monkeypatch):
    """SiteResult получает device, queue_wait, fetch_seconds из pipeline (включая DroidhubError)."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    _mock_async_client(monkeypatch, handler)

    site = SiteConfig("tutu", "https://tutu.test/{origin}{destination}", phone="realme")
    settings = Settings(
        sites=[site],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["realme"],
    )
    q = Query("MOW", "LED", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    res = results[0]
    assert res.ok is False
    assert res.device == "realme"
    assert res.queue_wait is not None
    assert res.fetch_seconds is not None


# ---------------------------------------------------------------------------
# 6. Миграция metrics и summary_by_device / summary_by_site_device
# ---------------------------------------------------------------------------


def test_metrics_migration_and_device_summaries(tmp_path):
    """Миграция добавляет device, queue_wait, fetch_seconds; summary_by_device строит сводку."""
    db_path = tmp_path / "metrics.sqlite"

    # Создаём старую схему без колонок device, queue_wait, fetch_seconds
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE site_runs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL NOT NULL,
                source   TEXT NOT NULL,
                site     TEXT NOT NULL,
                ok       INTEGER NOT NULL,
                partial  INTEGER NOT NULL,
                captcha  INTEGER NOT NULL,
                error    TEXT,
                offers   INTEGER NOT NULL,
                elapsed  REAL NOT NULL
            );
            INSERT INTO site_runs (ts, source, site, ok, partial, captcha, error, offers, elapsed)
            VALUES (100.0, 'user', 'old_site', 1, 0, 0, NULL, 5, 2.0);
            """
        )

    # Инициализируем MetricsStore: должна сработать идемпотентная миграция
    store = MetricsStore(db_path)
    # Повторная инициализация для проверки идемпотентности
    store._ensure_schema()

    # Проверяем, что старая запись имеет NULL в новых колонках
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT device, queue_wait, fetch_seconds FROM site_runs WHERE id = 1")
        row = cur.fetchone()
        assert row == (None, None, None)

    # Записываем новые результаты через record_results
    r1 = SiteResult(
        site="tutu",
        ok=True,
        offers=[],
        elapsed=2.5,
        device="realme",
        queue_wait=0.2,
        fetch_seconds=2.3,
    )
    r2 = SiteResult(
        site="tutu",
        ok=False,
        offers=[],
        error="капча",
        elapsed=1.0,
        device="realme",
        queue_wait=0.1,
        fetch_seconds=0.9,
    )
    r3 = SiteResult(
        site="onetwotrip",
        ok=True,
        offers=[],
        elapsed=1.8,
        device="redmi",
        queue_wait=0.05,
        fetch_seconds=1.75,
    )

    store.record_results(source="user", results=[r1, r2, r3], ts=200.0)

    # Проверяем summary_by_device
    dev_summ = store.summary_by_device(since_ts=150.0)
    assert "realme" in dev_summ
    assert "redmi" in dev_summ
    assert dev_summ["realme"]["runs"] == 2
    assert dev_summ["realme"]["ok"] == 1
    assert dev_summ["realme"]["failed"] == 1
    assert dev_summ["realme"]["captcha"] == 1
    assert dev_summ["realme"]["avg_fetch"] == 1.6  # (2.3 + 0.9) / 2
    assert dev_summ["realme"]["avg_queue_wait"] == 0.15  # (0.2 + 0.1) / 2

    assert dev_summ["redmi"]["runs"] == 1
    assert dev_summ["redmi"]["ok"] == 1

    # Проверяем summary_by_site_device
    site_dev_summ = store.summary_by_site_device(since_ts=150.0)
    assert ("tutu", "realme") in site_dev_summ
    assert site_dev_summ[("tutu", "realme")]["runs"] == 2
    assert site_dev_summ[("tutu", "realme")]["avg_fetch"] == 1.6


# ---------------------------------------------------------------------------
# 7. Панель разработчика: блок «Телефоны»
# ---------------------------------------------------------------------------


def test_admin_panel_telephones_block_with_data_and_without_data():
    """Панель корректно отображает блок «Телефоны» при наличии и отсутствии данных."""
    # 1. Данных нет
    ctx_no_devs = {
        "summary": {"_total": {"runs": 5}, "aviasales": {"runs": 5, "ok": 5}},
        "devices_summary": {},
    }
    txt_empty = admin_panel.build_parse(ctx_no_devs, "24h")
    assert "Телефоны: данных пока нет" in txt_empty
    assert len(txt_empty) <= 3800

    # 2. Данные есть
    ctx_with_devs = {
        "summary": {"_total": {"runs": 10}, "tutu": {"runs": 10, "ok": 8}},
        "devices_summary": {
            "realme": {
                "runs": 10,
                "ok": 8,
                "partial": 1,
                "captcha": 1,
                "failed": 2,
                "avg_fetch": 3.4,
                "avg_queue_wait": 0.5,
            }
        },
        "site_devices_summary": {
            ("tutu", "realme"): {
                "runs": 10,
                "ok": 8,
                "partial": 1,
                "captcha": 1,
                "failed": 2,
                "avg_fetch": 3.4,
                "avg_queue_wait": 0.5,
            }
        },
    }
    txt_full = admin_panel.build_parse(ctx_with_devs, "24h")
    assert "realme: 10 запусков · ок 80% · неполных 1 · капч 1 · ср. запрос 3.4 с · ср. ожидание 0.5 с" in txt_full
    assert "tutu × realme: 10 · ок 80% · неполн. 1 · капч 1 · 3.4 с" in txt_full
    assert len(txt_full) <= 3800
