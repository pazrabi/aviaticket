from __future__ import annotations

import asyncio
from datetime import date
from html import escape
import pytest

from avia.criteria import SearchCriteria
from avia.history import PriceHistory
from avia.models import Leg, Offer, Query, SiteResult
from avia.scheduler import Scheduler, build_notification, build_notification_markup
from avia.subscriptions import NotifyRules, Subscription, SubscriptionStore


class FakeClock:
    def __init__(self, start: float = 1700000000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@pytest.fixture
def store(tmp_path):
    return SubscriptionStore(tmp_path / "subs.db")


@pytest.fixture
def history(tmp_path):
    return PriceHistory(tmp_path / "history.db")


@pytest.fixture
def clock():
    return FakeClock(1700000000.0)


@pytest.fixture
def today():
    return lambda: date(2026, 12, 1)


@pytest.mark.asyncio
async def test_first_pass_records_price_and_does_not_notify(store, clock, today):
    """Первый проход пишет цену и не шлёт уведомление (нет эталона и минимума)."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    sent_notifications = []

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[
                    Offer(site="aviasales", price=10000, currency="RUB", url="https://site.ru/1"),
                ],
            )
        ]

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent_notifications.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 0
    assert len(sent_notifications) == 0

    # Проверяем, что цена записана
    assert store.first_price(sub.id) == 10000
    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.min_price == 10000
    assert sub_after.last_checked_at == clock()
    assert sub_after.last_notified_at is None


@pytest.mark.asyncio
async def test_second_pass_on_price_drop_notifies_with_reason(store, clock, today):
    """Второй проход при падении цены шлёт одно уведомление с причиной."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    current_price = 10000

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[
                    Offer(site="aviasales", price=current_price, currency="RUB", url="https://site.ru/1"),
                ],
            )
        ]

    sent_notifications = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent_notifications.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        clock=clock,
        today=today,
    )

    # Первый проход
    await sched.run_once()
    assert len(sent_notifications) == 0

    # Прошло время опроса (для 9 дней до вылета интервал 2700 с)
    clock.advance(3600)
    current_price = 8000  # падение на 2000 (20%)

    count = await sched.run_once()
    assert count == 1
    assert len(sent_notifications) == 1

    chat_id, text, markup = sent_notifications[0]
    assert chat_id == 101
    assert "🔔 <b>Цена изменилась</b>" in text
    assert "Причина:" in text
    assert "новый минимум" in text
    assert "упала на 2000 ₽ (20%)" in text

    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.min_price == 8000
    assert sub_after.last_notified_price == 8000
    assert sub_after.last_notified_at == clock()


@pytest.mark.asyncio
async def test_pause_prevents_second_notification(store, clock, today):
    """Пауза между уведомлениями не даёт второго уведомления, пока не прошло pause_seconds."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    current_price = 10000

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[
                    Offer(site="aviasales", price=current_price, currency="RUB", url="https://site.ru/1"),
                ],
            )
        ]

    sent_notifications = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent_notifications.append((chat_id, text, markup))

    rules = NotifyRules(pause_seconds=3 * 3600.0)
    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        rules=rules,
        clock=clock,
        today=today,
    )

    # 1. Первый проход (цена 10000)
    await sched.run_once()

    # 2. Второй проход: цена упала до 8000 через 3600 с -> уведомление отправлено
    clock.advance(3600)
    current_price = 8000
    count2 = await sched.run_once()
    assert count2 == 1
    assert len(sent_notifications) == 1

    # 3. Третий проход: цена упала ещё сильнее до 6000 через 3600 с (с последнего уведомления прошло 3600 < 10800)
    clock.advance(3600)
    current_price = 6000
    count3 = await sched.run_once()
    assert count3 == 0
    assert len(sent_notifications) == 1  # новых уведомлений нет

    # Но цена 6000 записана
    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.min_price == 6000


@pytest.mark.asyncio
async def test_same_route_subscriptions_single_search_call(store, clock, today):
    """Несколько подписок с одним маршрутом группируются в один вызов search_fn."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 15), adults=1)
    store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())
    store.create(chat_id=102, query=q, baggage_mode="with", criteria=SearchCriteria(), now=clock())

    search_calls = 0

    async def search_fn(query: Query) -> list[SiteResult]:
        nonlocal search_calls
        search_calls += 1
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[
                    Offer(site="aviasales", price=5000, currency="RUB", baggage="с багажом", url="https://s.ru/1"),
                ],
            )
        ]

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        pass

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        clock=clock,
        today=today,
    )

    await sched.run_once()
    assert search_calls == 1


@pytest.mark.asyncio
async def test_group_search_error_does_not_mark_checked_and_does_not_break_others(store, clock, today):
    """Ошибка поиска группы: не помечать проверенными и не ронять остальные группы."""
    q1 = Query(origin="MOW", destination="LED", depart=date(2026, 12, 15), adults=1)
    q2 = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 20), adults=1)

    sub1 = store.create(chat_id=101, query=q1, baggage_mode="any", criteria=SearchCriteria(), now=clock())
    sub2 = store.create(chat_id=102, query=q2, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    async def search_fn(query: Query) -> list[SiteResult]:
        if query.origin == "MOW":
            raise RuntimeError("Network error on MOW")
        return [
            SiteResult(
                site="tutu",
                ok=True,
                offers=[
                    Offer(site="tutu", price=7000, currency="RUB", url="https://tutu.ru/1"),
                ],
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

    sub1_after = store.get(sub1.id)
    assert sub1_after is not None
    assert sub1_after.last_checked_at is None  # НЕ помечена проверенной

    sub2_after = store.get(sub2.id)
    assert sub2_after is not None
    assert sub2_after.last_checked_at == clock()  # успешно обработана


@pytest.mark.asyncio
async def test_filters_and_baggage_affect_ticket_selection(store, clock, today):
    """Фильтры критериев и багажа влияют на выбор самого дешёвого билета для подписки."""
    q = Query(origin="MOW", destination="AER", depart=date(2026, 12, 10), adults=1)

    # sub1: любой багаж, только прямые рейсы
    sub1 = store.create(
        chat_id=101,
        query=q,
        baggage_mode="any",
        criteria=SearchCriteria(max_transfers=0),
        now=clock(),
    )
    # sub2: только с багажом, любые рейсы
    sub2 = store.create(
        chat_id=102,
        query=q,
        baggage_mode="with",
        criteria=SearchCriteria(),
        now=clock(),
    )

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="site_a",
                ok=True,
                offers=[
                    # 1 пересадка, без багажа: самый дешёвый в общем пуле
                    Offer(site="site_a", price=4000, currency="RUB", transfers=1, baggage="без багажа"),
                    # прямой рейс, без багажа
                    Offer(site="site_a", price=6000, currency="RUB", transfers=0, baggage="без багажа"),
                    # с пересадкой, с багажом
                    Offer(site="site_a", price=7000, currency="RUB", transfers=1, baggage="с багажом"),
                ],
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

    # sub1 должна выбрать прямой за 6000
    assert store.first_price(sub1.id) == 6000
    # sub2 должна выбрать с багажом за 7000
    assert store.first_price(sub2.id) == 7000


@pytest.mark.asyncio
async def test_no_matching_tickets_nothing_recorded(store, clock, today):
    """Если билетов нет (или отфильтрованы): store.mark_checked, цену не записывать."""
    q = Query(origin="MOW", destination="AER", depart=date(2026, 12, 10), adults=1)
    sub = store.create(
        chat_id=101,
        query=q,
        baggage_mode="with",
        criteria=SearchCriteria(max_transfers=0),
        now=clock(),
    )

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="site_a",
                ok=True,
                offers=[
                    # Не подходит: с пересадкой и без багажа
                    Offer(site="site_a", price=4000, currency="RUB", transfers=1, baggage="без багажа"),
                ],
            )
        ]

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=lambda *a, **k: None,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 0

    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.last_checked_at == clock()
    assert store.first_price(sub.id) is None
    assert sub_after.min_price is None


@pytest.mark.asyncio
async def test_notify_fn_failure_does_not_record_notification(store, clock, today):
    """Сбой notify_fn логируется, подписка не считается уведомлённой (record_notification не вызывается)."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    current_price = 10000

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=current_price, currency="RUB", url="https://s.ru/1")],
            )
        ]

    async def broken_notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        raise ConnectionError("Telegram API is unreachable")

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=broken_notify_fn,
        clock=clock,
        today=today,
    )

    # 1. Первый проход
    await sched.run_once()

    # 2. Второй проход: цена упала, notify_fn падает
    clock.advance(3600)
    current_price = 7000

    count = await sched.run_once()
    assert count == 0  # не увеличиваем счётчик отправленных

    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.last_notified_at is None
    assert sub_after.last_notified_price is None
    # Но цена записана
    assert store.recent_prices(sub.id, 0) == [10000, 7000]


@pytest.mark.asyncio
async def test_expired_subscription_deactivated_and_not_polled(store, clock, today):
    """Просроченная подписка деактивируется (expire_past) и не опрашивается."""
    # Вылет был 2026-11-20, а today = 2026-12-01
    past_q = Query(origin="MOW", destination="LED", depart=date(2026, 11, 20), adults=1)
    sub = store.create(chat_id=101, query=past_q, baggage_mode="any", criteria=SearchCriteria(), now=clock())
    assert sub.active is True

    search_called = False

    async def search_fn(query: Query) -> list[SiteResult]:
        nonlocal search_called
        search_called = True
        return []

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=lambda *a, **k: None,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 0
    assert search_called is False

    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.active is False


def test_build_notification_text_and_markup():
    """Тест build_notification: причины, ссылки, экранирование, длина <= 4096, разметка callback_data <= 64 байт."""
    crit = SearchCriteria(max_transfers=0, depart_parts=frozenset(["morning"]))
    sub = Subscription(
        id=42,
        chat_id=12345,
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 1),
        return_date=date(2026, 12, 4),
        adults=2,
        baggage_mode="with",
        criteria=crit,
    )
    offer = Offer(
        site="tutu",
        price=15000,
        currency="RUB",
        airline="Аэрофлот",
        depart_time="10:00",
        arrive_time="13:00",
        transfers=0,
        baggage="с багажом",
        url="https://avia.tutu.ru/offer/123?foo=1&bar=2",
        url_is_direct=True,
    )
    reasons = ["новый минимум", "упала на 2000 ₽ (12%)"]

    text = build_notification(sub, offer, reasons)
    lines = text.split("\n")

    assert lines[0] == "🔔 <b>Цена изменилась</b>"
    assert lines[1] == "✈️ <b>Самара → Ереван</b>"
    assert lines[2] == "📅 1 дек – 4 дек  ·  👤 2"
    assert lines[4] == "📈 Причина: новый минимум; упала на 2000 ₽ (12%)"
    assert "15 000 ₽" in text.replace("\xa0", " ")
    assert "<i>Туту</i>" in text
    assert '<a href="https://avia.tutu.ru/offer/123?foo=1&amp;bar=2">купить билет</a>' in text
    assert "Озон" not in text
    assert "пересадки: только прямые, вылет: утром" in lines[-1]
    assert len(text) <= 4096

    # Разметка
    markup = build_notification_markup(sub)
    assert "inline_keyboard" in markup
    assert len(markup["inline_keyboard"]) == 3

    r1, r2, r3 = markup["inline_keyboard"]
    assert r1[0]["text"] == "🎯 Порог цены"
    assert r1[0]["callback_data"] == "thr:42"
    assert r1[1]["text"] == "🔕 Отписаться"
    assert r1[1]["callback_data"] == "unsub:42"
    assert r2[0]["text"] == "📊 История"
    assert r2[0]["callback_data"] == "hist:sub:42"
    assert r3[0]["text"] == "🔎 Новый поиск"
    assert r3[0]["callback_data"] == "menu:go"

    for row in markup["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_build_notification_one_way_and_non_direct_and_no_criteria():
    """Тест build_notification для билета в одну сторону, непрямой ссылки и без критериев."""
    sub = Subscription(
        id=99,
        chat_id=54321,
        origin="LED",
        destination="MOW",
        depart=date(2026, 12, 10),
        return_date=None,
        adults=1,
        baggage_mode="any",
        criteria=SearchCriteria(),
    )
    offer = Offer(
        site="yandex_travel",
        price=3500,
        currency="RUB",
        airline="S7",
        depart_time="08:00",
        arrive_time="09:30",
        transfers=0,
        url="https://travel.yandex.ru/avia/search",
        url_is_direct=False,
    )
    text = build_notification(sub, offer, ["на 15% ниже обычного"])
    lines = text.split("\n")

    assert lines[1] == "✈️ <b>Санкт-Петербург → Москва</b>"
    assert lines[2] == "📅 10 дек  ·  👤 1"
    assert lines[4] == "📈 Причина: на 15% ниже обычного"
    assert '<a href="https://travel.yandex.ru/avia/search">открыть выдачу сайта</a>' in text
    # Без критериев строки Фильтры нет
    assert not any(l.startswith("Фильтры:") for l in lines)


def test_build_notification_escaping_and_length():
    """Экранирование спецсимволов HTML и ограничение длины 4096."""
    sub = Subscription(
        id=1,
        chat_id=1,
        origin="<FOO>",
        destination="BAR&BAZ",
        depart=date(2026, 12, 1),
        adults=1,
    )
    offer = Offer(
        site="site<1>",
        price=1000,
        url="https://test.com/link?a=1&b=2",
    )
    reasons = ["причина <1> & <2>"]
    text = build_notification(sub, offer, reasons)

    assert "&lt;FOO&gt;" in text
    assert "BAR&amp;BAZ" in text
    assert "site&lt;1&gt;" in text
    assert "причина &lt;1&gt; &amp; &lt;2&gt;" in text
    assert "a=1&amp;b=2" in text

    # Очень длинные причины
    huge_reasons = ["очень длинная причина " * 300]
    long_text = build_notification(sub, offer, huge_reasons)
    assert len(long_text) <= 4096


@pytest.mark.asyncio
async def test_run_forever_survives_exception_and_cancels(store, clock, today):
    """run_forever переживает исключение прохода (через короткий tick и отмену задачи)."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    calls = 0

    async def flaky_search(query: Query) -> list[SiteResult]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Первый сбой прохода")
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=10000, currency="RUB")],
            )
        ]

    sched = Scheduler(
        store=store,
        search_fn=flaky_search,
        notify_fn=lambda *a, **k: None,
        clock=clock,
        today=today,
    )

    task = asyncio.create_task(sched.run_forever(tick_seconds=0.01))

    # Ждём немного, чтобы цикл успел сделать несколько итераций
    await asyncio.sleep(0.06)

    assert calls >= 2  # пережил исключение и сделал вторую итерацию
    assert not task.done()

    # Отмена задачи: CancelledError пробрасывается
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_history_annotate_integration(store, history, clock, today):
    """Проверка вызова PriceHistory.annotate через asyncio.to_thread и устойчивость к ошибке истории."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), adults=1)
    store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[
                    Offer(site="aviasales", price=10000, currency="RUB", url="https://s.ru/1"),
                ],
            )
        ]

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=lambda *a, **k: None,
        history=history,
        clock=clock,
        today=today,
    )

    await sched.run_once()

    # Проверяем, что в history записалась цена
    with history._connect() as db:
        row = db.execute("SELECT price, currency FROM price_history").fetchone()
        assert row is not None
        assert row[0] == 10000
        assert row[1] == "RUB"


@pytest.mark.asyncio
async def test_scheduler_max_groups_per_run_and_staleness_order(store, clock, today):
    """Ограничение числа групп за проход и порядок по давности последней проверки."""
    # Создаём 6 групп с разными направлениями
    cities = ["LED", "AER", "OVB", "SVX", "KZN", "ROV"]
    subs = []
    for i, city in enumerate(cities, start=1):
        q = Query(origin="MOW", destination=city, depart=date(2026, 12, 10), adults=1)
        sub = store.create(chat_id=100 + i, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())
        subs.append(sub)

    # Задаём разную давность проверки (все старше интервала опроса ~2700 с, чтобы были due):
    # sub[0] (LED): None (никогда не проверялась -> самая просроченная)
    # sub[1] (AER): 8000 с назад (давняя)
    # sub[2] (OVB): 7000 с назад
    # sub[3] (SVX): 6000 с назад
    # sub[4] (KZN): 5000 с назад
    # sub[5] (ROV): 4000 с назад (самая свежая)
    now_ts = clock()
    store.mark_checked(subs[1].id, now=now_ts - 8000)
    store.mark_checked(subs[2].id, now=now_ts - 7000)
    store.mark_checked(subs[3].id, now=now_ts - 6000)
    store.mark_checked(subs[4].id, now=now_ts - 5000)
    store.mark_checked(subs[5].id, now=now_ts - 4000)

    searched_destinations: list[str] = []

    async def search_fn(query: Query) -> list[SiteResult]:
        searched_destinations.append(query.destination)
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
        max_groups_per_run=3,
    )

    # Первый проход: должно выполниться ровно 3 поиска в порядке давности: LED (None), AER (-8000), OVB (-7000)
    await sched.run_once()
    assert searched_destinations == ["LED", "AER", "OVB"]

    # Второй проход: следующие по давности SVX (-6000), KZN (-5000), ROV (-4000)
    searched_destinations.clear()
    await sched.run_once()
    assert searched_destinations == ["SVX", "KZN", "ROV"]

