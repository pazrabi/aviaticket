"""Тесты очереди подписок по сроку вылета и обработки вытесненных поисков."""
from __future__ import annotations

import asyncio
from datetime import date
import pytest

from avia.criteria import SearchCriteria
from avia.models import Offer, Query, SiteResult
from avia.scheduler import Scheduler
from avia.subscriptions import SubscriptionStore


class FakeClock:
    def __init__(self, current: float = 1700000000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@pytest.fixture
def store(tmp_path):
    return SubscriptionStore(tmp_path / "subs.db")


@pytest.fixture
def clock():
    return FakeClock(1700000000.0)


@pytest.fixture
def today():
    return lambda: date(2026, 12, 1)


@pytest.mark.asyncio
async def test_scheduler_groups_sorted_by_departure_date(store, clock, today):
    """(Б) Группы подписок сортируются по сроку вылета: сначала те, у которых вылет ближе."""
    # 3 группы с разными датами вылета:
    # sub1: вылет через 24 дня (2026-12-25)
    # sub2: вылет через 4 дня (2026-12-05) - самый ранний!
    # sub3: вылет через 14 дней (2026-12-15)
    q1 = Query(origin="MOW", destination="LED", depart=date(2026, 12, 25), adults=1)
    sub1 = store.create(chat_id=101, query=q1, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    q2 = Query(origin="MOW", destination="AER", depart=date(2026, 12, 5), adults=1)
    sub2 = store.create(chat_id=102, query=q2, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    q3 = Query(origin="MOW", destination="OVB", depart=date(2026, 12, 15), adults=1)
    sub3 = store.create(chat_id=103, query=q3, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # Делаем давность у sub1 самой «старой» (10000 с назад), но вылет дальше всех.
    # sub2 проверялась позже (5000 с назад), но вылет ближе всех.
    now_ts = clock()
    store.mark_checked(sub1.id, now=now_ts - 10000)
    store.mark_checked(sub2.id, now=now_ts - 5000)
    store.mark_checked(sub3.id, now=now_ts - 8000)

    searched_order: list[str] = []

    async def search_fn(query: Query) -> list[SiteResult]:
        searched_order.append(query.destination)
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=5000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=lambda *a, **k: None,
        clock=clock,
        today=today,
    )

    await sched.run_once()

    # Порядок должен быть по дате вылета: AER (12-05) -> OVB (12-15) -> LED (12-25)
    assert searched_order == ["AER", "OVB", "LED"]


@pytest.mark.asyncio
async def test_scheduler_groups_tie_broken_by_staleness(store, clock, today):
    """(Б) При одинаковой дате вылета группы сортируются по давности проверки."""
    dep = date(2026, 12, 20)
    q1 = Query(origin="MOW", destination="LED", depart=dep, adults=1)
    sub1 = store.create(chat_id=101, query=q1, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    q2 = Query(origin="MOW", destination="AER", depart=dep, adults=1)
    sub2 = store.create(chat_id=102, query=q2, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    now_ts = clock()
    # sub2 проверялась 8000 с назад (более просроченная), sub1 — 4000 с назад
    store.mark_checked(sub1.id, now=now_ts - 4000)
    store.mark_checked(sub2.id, now=now_ts - 8000)

    searched_order: list[str] = []

    async def search_fn(query: Query) -> list[SiteResult]:
        searched_order.append(query.destination)
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=5000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=lambda *a, **k: None,
        clock=clock,
        today=today,
    )

    await sched.run_once()

    # Даты вылета равны -> первыми идут более просроченные (AER перед LED)
    assert searched_order == ["AER", "LED"]


@pytest.mark.asyncio
async def test_scheduler_preempted_search_does_not_mark_checked(store, clock, today):
    """(В) Если из-за вытеснения results пусты, mark_checked не вызывается и подписка повторяется."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())
    assert sub.last_checked_at is None

    # Поиск возвращает пустой список результатов (все сайты вытеснены)
    call_count = 0

    async def preempted_search(query: Query) -> list[SiteResult]:
        nonlocal call_count
        call_count += 1
        return []

    sched = Scheduler(
        store=store,
        search_fn=preempted_search,
        notify_fn=lambda *a, **k: None,
        clock=clock,
        today=today,
    )

    sent = await sched.run_once()
    assert sent == 0
    assert call_count == 1

    # Подписка НЕ должна быть отмечена проверенной!
    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.last_checked_at is None

    # На следующем тике она снова будет due и повторится
    due_subs = store.due(clock(), today())
    assert any(s.id == sub.id for s in due_subs)

    # Теперь имитируем успешный поиск на следующем тике
    async def ok_search(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=8000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sched.search_fn = ok_search
    await sched.run_once()

    # Теперь подписка успешно проверена
    sub_final = store.get(sub.id)
    assert sub_final is not None
    assert sub_final.last_checked_at is not None
