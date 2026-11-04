"""Тесты аналитики и медианы маршрута в планировщике (Шаг 2 ROADMAP)."""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import MagicMock
import pytest

from avia import analytics
from avia.criteria import SearchCriteria
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult
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


def _seed_history_searches(
    history: PriceHistory,
    query: Query,
    prices_with_sites: list[tuple[int, str]],
    base_time: float,
) -> None:
    """Заполняет историю цен отдельными замерами поиска."""
    for i, (price, site) in enumerate(prices_with_sites, start=1):
        ts = base_time - (len(prices_with_sites) - i + 1) * 3600.0
        offer = Offer(
            site=site,
            price=price,
            currency=query.currency,
            offer_id=f"off_{i}",
            url=f"https://{site}.ru/{i}",
        )
        history.annotate(
            q=query,
            results=[SiteResult(site=site, ok=True, offers=[offer])],
            now=ts,
            search_id=f"search_{i}",
        )


# --- 1. Тесты медианы маршрута ---


@pytest.mark.asyncio
async def test_route_median_triggers_when_enough_measurements(store, history, clock, today):
    """При достаточном числе замеров маршрута (>= 5) срабатывает правило 'на N% ниже обычного'."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # 4 замера в истории по 10000 ₽ (разные search_id)
    _seed_history_searches(
        history,
        q,
        [(10000, "aviasales")] * 4,
        base_time=clock(),
    )

    # Текущий замер в Scheduler: цена 7000 ₽ (на 30% ниже 10000 ₽)
    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=7000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]
    assert "на 30% ниже обычного" in text


@pytest.mark.asyncio
async def test_route_median_does_not_trigger_when_not_enough_measurements(store, history, clock, today):
    """При недостаточном числе замеров маршрута (< 5) правило 'ниже обычного' не срабатывает."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # Только 2 замера в истории по 10000 ₽ (+ 1 текущий = 3 < 5)
    _seed_history_searches(
        history,
        q,
        [(10000, "aviasales")] * 2,
        base_time=clock(),
    )

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=7000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=clock,
        today=today,
    )

    # Без достаточного числа точек для медианы и без первого эталона подписки уведомление не шлётся
    count = await sched.run_once()
    assert count == 0
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_route_median_fallback_without_history(store, clock, today):
    """Без history планировщик использует recent_prices из SubscriptionStore."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # Записываем 5 цен в sub_prices (история самой подписки)
    for i in range(5):
        store.record_price(sub.id, 10000, "RUB", f"o{i}", clock() - 3600 * (5 - i))

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=7000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=None,  # history отсутствует
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]
    assert "на 30% ниже обычного" in text


@pytest.mark.asyncio
async def test_route_median_fallback_on_history_error(store, history, clock, today):
    """При ошибке history.route_search_minima планировщик откатывается на SubscriptionStore."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # В истории подписки 5 замеров
    for i in range(5):
        store.record_price(sub.id, 10000, "RUB", f"o{i}", clock() - 3600 * (5 - i))

    # Ломаем route_search_minima
    history.route_search_minima = MagicMock(side_effect=RuntimeError("SQLite I/O failure"))

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=7000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]
    assert "на 30% ниже обычного" in text


# --- 2. Тесты extra_lines в build_notification ---


def test_build_notification_extra_lines_position_and_hint_last():
    """extra_lines добавляются отдельным блоком после карточки, а подсказка остаётся последней."""
    sub = Subscription(
        id=1,
        chat_id=100,
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
    )
    offer = Offer(
        site="aviasales",
        price=11000,
        currency="RUB",
        airline="Аэрофлот",
        depart_time="10:00",
        transfers=0,
        url="https://s.ru/1",
        url_is_direct=True,
    )
    extra = [
        "💚 Дешевле, чем в 85% замеров по этому маршруту",
        "📈 За 3 и 7 дней растёт",
    ]
    hint = "Если планируете лететь, возможно, стоит не откладывать покупку."

    # Случай роста цены: есть подсказка hint
    text_rise = build_notification(
        sub,
        offer,
        reasons=["подорожала на 1000 ₽ (10%)"],
        extra_lines=extra,
    )
    lines_rise = text_rise.split("\n")

    # Подсказка должна быть последней строкой
    assert lines_rise[-1] == hint

    # extra_lines должны быть отдельным блоком перед подсказкой
    extra_idx = lines_rise.index(extra[0])
    assert lines_rise[extra_idx - 1] == ""  # пустая строка перед блоком аналитики
    assert lines_rise[extra_idx] == extra[0]
    assert lines_rise[extra_idx + 1] == extra[1]
    assert lines_rise[extra_idx + 2] == ""  # пустая строка перед подсказкой
    assert lines_rise[extra_idx + 3] == hint

    # Случай падения цены: подсказки hint нет, extra_lines идут после карточки билета
    text_drop = build_notification(
        sub,
        offer,
        reasons=["новый минимум"],
        extra_lines=extra,
    )
    lines_drop = text_drop.split("\n")
    assert hint not in text_drop
    drop_extra_idx = lines_drop.index(extra[0])
    assert lines_drop[drop_extra_idx - 1] == ""
    assert lines_drop[drop_extra_idx] == extra[0]
    assert lines_drop[drop_extra_idx + 1] == extra[1]
    assert lines_drop[-1] == extra[1]


def test_build_notification_extra_lines_with_criteria():
    """Если у подписки есть критерии, hint остаётся последней строкой."""
    sub = Subscription(
        id=1,
        chat_id=100,
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
        criteria=SearchCriteria(max_transfers=0),
    )
    offer = Offer(
        site="aviasales",
        price=11000,
        currency="RUB",
        airline="Аэрофлот",
        depart_time="10:00",
        transfers=0,
        url="https://s.ru/1",
        url_is_direct=True,
    )
    extra = ["💚 Дешевле, чем в 85% замеров"]
    hint = "Если планируете лететь, возможно, стоит не откладывать покупку."

    text = build_notification(
        sub,
        offer,
        reasons=["подорожала на 1000 ₽ (10%)"],
        extra_lines=extra,
    )
    lines = text.split("\n")
    assert lines[-1] == hint
    assert any("пересадки: только прямые" in l for l in lines)
    assert extra[0] in lines


def test_build_notification_without_extra_lines_identical():
    """Вызов без extra_lines (None или пустой список) не добавляет лишних пустых строк."""
    sub = Subscription(
        id=1,
        chat_id=100,
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
    )
    offer = Offer(site="aviasales", price=10000, currency="RUB", url="https://s.ru/1")
    reasons = ["новый минимум"]

    t_default = build_notification(sub, offer, reasons)
    t_none = build_notification(sub, offer, reasons, extra_lines=None)
    t_empty = build_notification(sub, offer, reasons, extra_lines=[])

    assert t_default == t_none
    assert t_default == t_empty


# --- 3. Тесты интеграции аналитики в Scheduler.run_once ---


@pytest.mark.asyncio
async def test_scheduler_run_once_includes_analytics_summary(store, history, clock, today):
    """При наличии истории планировщик добавляет до 3 строк аналитики в уведомление."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # Заполняем замеры по разным сайтам и ценам (минимум 5 с участием Купибилет)
    _seed_history_searches(
        history,
        q,
        [
            (12000, "tutu"),
            (11500, "aviasales"),
            (11000, "kupibilet"),
            (10500, "kupibilet"),
            (10000, "kupibilet"),
            (9500, "kupibilet"),
            (9000, "kupibilet"),
        ],
        base_time=clock(),
    )

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="kupibilet",
                ok=True,
                offers=[Offer(site="kupibilet", price=7000, currency="RUB", url="https://k.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]

    # Проверяем строки аналитики
    assert "Дешевле, чем в" in text
    assert "Чаще всего дешевле на Купибилет" in text


@pytest.mark.asyncio
async def test_scheduler_run_once_analytics_error_does_not_break_notify(store, history, clock, today):
    """Сбой в history.route_stats или cheapest_site_stats логируется, уведомление отправляется без extra_lines."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    # В истории 5 замеров по 10000 ₽ (чтобы сработало правило медианы)
    _seed_history_searches(
        history,
        q,
        [(10000, "aviasales")] * 5,
        base_time=clock(),
    )

    # Ломаем route_stats
    history.route_stats = MagicMock(side_effect=RuntimeError("Corrupt route stats"))

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=7000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]

    # Уведомление отправлено успешно, но без строк аналитики
    assert "на 30% ниже обычного" in text
    assert "Дешевле, чем в" not in text


@pytest.mark.asyncio
async def test_scheduler_run_once_cheapest_site_stats_error_does_not_break_notify(store, history, clock, today):
    """Сбой в history.cheapest_site_stats логируется, уведомление отправляется без extra_lines."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock())

    _seed_history_searches(
        history,
        q,
        [(10000, "aviasales")] * 5,
        base_time=clock(),
    )

    history.cheapest_site_stats = MagicMock(side_effect=RuntimeError("Corrupt cheapest_site_stats"))

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=7000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=clock,
        today=today,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]

    assert "на 30% ниже обычного" in text
    assert "Дешевле, чем в" not in text
    assert "Чаще всего дешевле на" not in text


# --- 4. Тесты кнопки «📊 История» ---


def test_build_notification_markup_history_button():
    """В разметке уведомления есть отдельная строка с кнопкой '📊 История' перед '🔎 Новый поиск'."""
    sub = Subscription(
        id=77,
        chat_id=123,
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
    )
    markup = build_notification_markup(sub)
    kb = markup.get("inline_keyboard", [])
    assert len(kb) == 3

    # Строка 0: порог и отписка
    row0 = kb[0]
    assert len(row0) == 2
    assert row0[0]["text"] == "🎯 Порог цены"
    assert row0[0]["callback_data"] == "thr:77"
    assert row0[1]["text"] == "🔕 Отписаться"
    assert row0[1]["callback_data"] == "unsub:77"

    # Строка 1: история
    row1 = kb[1]
    assert len(row1) == 1
    assert row1[0]["text"] == "📊 История"
    assert row1[0]["callback_data"] == "hist:sub:77"

    # Строка 2: новый поиск
    row2 = kb[2]
    assert len(row2) == 1
    assert row2[0]["text"] == "🔎 Новый поиск"
    assert row2[0]["callback_data"] == "menu:go"

    for row in kb:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
