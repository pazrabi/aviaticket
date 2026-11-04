"""Тесты для PriorityGate."""
from __future__ import annotations

import asyncio
import pytest

from avia.prio_gate import PriorityGate


@pytest.mark.asyncio
async def test_capacity_and_active():
    """Ёмкость и счётчик активных слотов."""
    gate = PriorityGate(2)
    assert gate.capacity == 2
    assert gate.active() == 0
    assert gate.waiting_by_priority() == {}

    async with gate.slot(0):
        assert gate.active() == 1
        async with gate.slot(1):
            assert gate.active() == 2
        assert gate.active() == 1
    assert gate.active() == 0


@pytest.mark.asyncio
async def test_priority_order_and_fifo():
    """Слоты выдаются по (priority, arrival_order): 0 важнее 1 и 2, внутри приоритета — FIFO."""
    gate = PriorityGate(1)
    order: list[str] = []

    # Занимаем единственный слот
    acquired_first = asyncio.Event()
    release_first = asyncio.Event()

    async def first_task():
        async with gate.slot(2):
            acquired_first.set()
            order.append("first_running")
            await release_first.wait()
            order.append("first_done")

    t_first = asyncio.create_task(first_task())
    await acquired_first.wait()

    # Пока первый работает, ставим в очередь:
    # 1. подписка 1 (пришла первой среди ожидающих)
    # 2. фон 2
    # 3. человек 0 (пришёл позже, но приоритет выше)
    # 4. ещё одна подписка 1 (пришла второй среди подписок)

    async def task_sub_1():
        async with gate.slot(1):
            order.append("sub_1")

    async def task_bg():
        async with gate.slot(2):
            order.append("bg")

    async def task_human():
        async with gate.slot(0):
            order.append("human")

    async def task_sub_2():
        async with gate.slot(1):
            order.append("sub_2")

    t_sub_1 = asyncio.create_task(task_sub_1())
    await asyncio.sleep(0.01)

    t_bg = asyncio.create_task(task_bg())
    await asyncio.sleep(0.01)

    t_human = asyncio.create_task(task_human())
    await asyncio.sleep(0.01)

    t_sub_2 = asyncio.create_task(task_sub_2())
    await asyncio.sleep(0.01)

    # Проверяем состояние очереди
    waiting = gate.waiting_by_priority()
    assert waiting.get(0) == 1
    assert waiting.get(1) == 2
    assert waiting.get(2) == 1

    # Освобождаем первый слот
    release_first.set()

    await asyncio.gather(t_first, t_sub_1, t_bg, t_human, t_sub_2)

    # Ожидаемый порядок:
    # first_running -> first_done -> human (prio 0) -> sub_1 (prio 1, FIFO) -> sub_2 (prio 1, FIFO) -> bg (prio 2)
    assert order == [
        "first_running",
        "first_done",
        "human",
        "sub_1",
        "sub_2",
        "bg",
    ]
    assert gate.active() == 0
    assert gate.waiting_by_priority() == {}


@pytest.mark.asyncio
async def test_non_preemption():
    """Уже идущий поиск не вытесняется более приоритетным поиском."""
    gate = PriorityGate(1)
    status: list[str] = []
    bg_started = asyncio.Event()
    bg_finish = asyncio.Event()

    async def bg_task():
        async with gate.slot(2):
            status.append("bg_started")
            bg_started.set()
            await bg_finish.wait()
            status.append("bg_finished")

    t_bg = asyncio.create_task(bg_task())
    await bg_started.wait()

    human_started = False

    async def human_task():
        nonlocal human_started
        async with gate.slot(0):
            human_started = True
            status.append("human_run")

    t_human = asyncio.create_task(human_task())
    await asyncio.sleep(0.05)

    # Человек ждёт, фоновый поиск не прервался
    assert not human_started
    assert status == ["bg_started"]

    bg_finish.set()
    await asyncio.gather(t_bg, t_human)

    assert status == ["bg_started", "bg_finished", "human_run"]


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_lose_slot():
    """Отмена ожидающей задачи удаляет её из очереди и не теряет слот."""
    gate = PriorityGate(1)
    held = asyncio.Event()
    release_held = asyncio.Event()

    async def blocker():
        async with gate.slot(0):
            held.set()
            await release_held.wait()

    t_block = asyncio.create_task(blocker())
    await held.wait()

    # Задача 1 (будет отменена)
    async def cancelled_waiter():
        async with gate.slot(0):
            pass

    t_cancel = asyncio.create_task(cancelled_waiter())
    await asyncio.sleep(0.01)

    assert gate.waiting_by_priority().get(0) == 1

    # Задача 2 (следующая в очереди)
    reached_next = False

    async def next_waiter():
        nonlocal reached_next
        async with gate.slot(1):
            reached_next = True

    t_next = asyncio.create_task(next_waiter())
    await asyncio.sleep(0.01)

    # Отменяем t_cancel
    t_cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t_cancel

    # Проверяем, что отменённая задача ушла из очереди
    assert gate.waiting_by_priority().get(0, 0) == 0
    assert gate.waiting_by_priority().get(1) == 1

    # Освобождаем блокер
    release_held.set()
    await t_block

    # Следующая задача должна успешно получить слот
    await t_next
    assert reached_next
    assert gate.active() == 0


@pytest.mark.asyncio
async def test_exception_in_block_releases_slot():
    """Исключение внутри блока освобождает слот для следующих ожидающих."""
    gate = PriorityGate(1)

    with pytest.raises(RuntimeError, match="test_error"):
        async with gate.slot(0):
            assert gate.active() == 1
            raise RuntimeError("test_error")

    assert gate.active() == 0

    next_done = False
    async with gate.slot(1):
        assert gate.active() == 1
        next_done = True

    assert next_done
    assert gate.active() == 0
