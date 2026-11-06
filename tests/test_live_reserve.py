"""Тесты резерва телефонов под живой поиск (PLAN_SPEED шаг 10)."""
from __future__ import annotations

import asyncio
import os
import pytest

from avia.config import Settings, load_settings
from avia.droidhub import (
    PreferenceList,
    _busy_devices,
    _dispatch,
    _reset_pool,
    _waiters,
    acquire,
    advance_time,
    release,
    set_clock,
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


def test_live_reserve_settings_default_and_env(monkeypatch):
    """Флаг LIVE_RESERVE по умолчанию включён (1), Settings.live_reserve=True, при 0 отключается."""
    s_default = Settings(sites=[])
    assert s_default.live_reserve is True

    monkeypatch.setenv("LIVE_RESERVE", "0")
    s_off = load_settings()
    assert s_off.live_reserve is False

    monkeypatch.setenv("LIVE_RESERVE", "1")
    s_on = load_settings()
    assert s_on.live_reserve is True


@pytest.mark.asyncio
async def test_live_reserve_blocks_bg_from_taking_last_phone():
    """Если среди >= 3 кандидатов остался 1 свободный телефон, priority>=1 ждёт, priority 0 забирает."""
    # Занимаем 2 телефона из 3
    await acquire(["d1"], priority=0)
    await acquire(["d2"], priority=0)
    assert _busy_devices == {"d1", "d2"}

    # Фоновый запрос (priority=1) с кандидатами d1, d2, d3
    bg_task = asyncio.create_task(acquire(["d1", "d2", "d3"], priority=1))
    await asyncio.sleep(0.01)

    # Фоновый запрос остаётся в очереди, d3 не занят
    assert not bg_task.done()
    assert "d3" not in _busy_devices

    # Человек (priority=0) запрашивает телефон
    live_dev = await acquire(["d1", "d2", "d3"], priority=0)
    assert live_dev == "d3"
    assert "d3" in _busy_devices

    # Фон по-прежнему ждёт (все 3 телефона заняты)
    assert not bg_task.done()

    # Человек освобождает d3
    await release("d3")
    await asyncio.sleep(0.01)

    # d1 и d2 всё ещё заняты, d3 снова последний свободный -> фон по-прежнему ждёт
    assert not bg_task.done()
    assert "d3" not in _busy_devices

    # Освобождаем d2 (теперь свободны d2 и d3: 2 свободных телефона >= 2)
    await release("d2")
    await asyncio.sleep(0.01)

    # Фон получил освободившийся телефон
    assert bg_task.done()
    acquired_by_bg = bg_task.result()
    assert acquired_by_bg in ("d2", "d3")
    # После выдачи фону остался как минимум 1 свободный телефон
    remaining_free = {"d1", "d2", "d3"} - _busy_devices
    assert len(remaining_free) >= 1

    await release(acquired_by_bg)
    await release("d1")


@pytest.mark.asyncio
async def test_live_reserve_ignored_when_candidates_less_than_three():
    """Если кандидатов < 3 — резерва нет (иначе фон вообще не работал бы)."""
    # 2 кандидата, 1 занят
    await acquire(["d1"], priority=0)
    assert "d1" in _busy_devices

    # Фон (priority=1) с 2 кандидатами [d1, d2] получает последний свободный (d2)
    dev = await acquire(["d1", "d2"], priority=1)
    assert dev == "d2"
    assert "d2" in _busy_devices

    await release("d1")
    await release("d2")


@pytest.mark.asyncio
async def test_live_reserve_disabled_via_flag():
    """При live_reserve=False / LIVE_RESERVE=0 старое поведение: фон забирает последний телефон."""
    await acquire(["d1"], priority=0)
    await acquire(["d2"], priority=0)
    assert _busy_devices == {"d1", "d2"}

    # С выключенным резервом фон получает d3
    dev = await acquire(["d1", "d2", "d3"], priority=1, live_reserve=False)
    assert dev == "d3"
    assert "d3" in _busy_devices

    await release("d1")
    await release("d2")
    await release("d3")


@pytest.mark.asyncio
async def test_live_reserve_preserves_patience_and_prefer_order():
    """Терпение и порядок предпочтения не ломаются резервом."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # Занимаем d1
    await acquire(["d1"], priority=0)

    # d2 и d3 свободны (2 телефона свободны из 3, резерв позволяет выдать 1 телефон фону)
    pref = PreferenceList(["d1", "d2", "d3"], patience=10.0)
    task = asyncio.create_task(acquire(pref, priority=1, prefer_order=True))
    await asyncio.sleep(0.001)

    # Терпение активно: d1 занят, поэтому на d2/d3 не переходит до дедлайна
    assert not task.done()
    assert "d2" not in _busy_devices
    assert "d3" not in _busy_devices

    # Продвигаем время на 10 секунд (терпение истекло)
    advance_time(10.0)
    await asyncio.sleep(0.001)

    # Терпение вышло: фон получает d2 (предпочтительный из оставшихся)
    assert task.done()
    assert task.result() == "d2"
    # d3 остался свободным под живой поиск
    assert "d3" not in _busy_devices

    await release("d1")
    await release("d2")
