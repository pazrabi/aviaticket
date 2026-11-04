from __future__ import annotations

import asyncio
from datetime import date
import pytest

from avia.criteria import SearchCriteria
from avia.models import Offer, Query, SiteResult
from avia.scheduler import Scheduler, build_notification, build_notification_markup
from avia.subscriptions import (
    NotifyRules,
    Subscription,
    SubscriptionStore,
    evaluate,
)


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
def clock():
    return FakeClock(1700000000.0)


@pytest.fixture
def today():
    return lambda: date(2026, 12, 1)


def _make_sub(**kwargs) -> Subscription:
    params = {
        "id": 1,
        "chat_id": 100,
        "origin": "MOW",
        "destination": "LED",
        "depart": date(2026, 12, 1),
        "return_date": None,
        "adults": 1,
        "baggage_mode": "any",
        "criteria": SearchCriteria(),
        "threshold": None,
        "active": True,
        "created_at": 0.0,
        "last_checked_at": None,
        "last_notified_at": None,
        "last_notified_price": None,
        "last_notified_offer_id": None,
        "min_price": None,
    }
    params.update(kwargs)
    return Subscription(**params)


def test_notify_rules_rise_pct_default_and_custom():
    rules_default = NotifyRules()
    assert rules_default.rise_pct == 10.0

    rules_custom = NotifyRules(rise_pct=15.0)
    assert rules_custom.rise_pct == 15.0


def test_price_rise_ten_percent_gives_reason():
    """Рост ровно на 10% (10000 → 11000) даёт причину «подорожала на 1000 ₽ (10%)»."""
    sub = _make_sub(last_notified_price=10000)
    reasons = evaluate(
        sub=sub,
        price=11000,
        offer_id="off_new",
        now=1000.0,
        recent_prices=[],
    )
    assert reasons == ["подорожала на 1000 ₽ (10%)"]


def test_price_rise_nine_percent_no_reason():
    """Рост на 9% (10000 → 10900) и 9.99% (10000 → 10999) не даёт причину."""
    sub = _make_sub(last_notified_price=10000)
    assert evaluate(sub, price=10900, offer_id="off_new", now=1000.0, recent_prices=[]) == []
    assert evaluate(sub, price=10999, offer_id="off_new", now=1000.0, recent_prices=[]) == []


def test_price_rise_float_precision():
    """Проверка точности float: 3000 * 1.1 может давать 3300.0000000000005, но 3300 должно срабатывать."""
    sub = _make_sub(last_notified_price=3000)
    reasons = evaluate(sub, price=3300, offer_id="off_new", now=1000.0, recent_prices=[])
    assert reasons == ["подорожала на 300 ₽ (10%)"]


def test_price_rise_custom_threshold():
    """Кастомный порог роста rise_pct: при 15% рост на 10% не срабатывает, а на 15% срабатывает."""
    rules = NotifyRules(rise_pct=15.0)
    sub = _make_sub(last_notified_price=10000)
    assert evaluate(sub, price=11000, offer_id="off_new", now=1000.0, recent_prices=[], rules=rules) == []

    reasons = evaluate(sub, price=11500, offer_id="off_new", now=1000.0, recent_prices=[], rules=rules)
    assert reasons == ["подорожала на 1500 ₽ (15%)"]


def test_last_notified_price_priority_over_reference_price():
    """Эталон last_notified_price приоритетнее reference_price."""
    # Если last_notified_price задан (10000), reference_price (8000) игнорируется.
    sub_notified = _make_sub(last_notified_price=10000)
    # 10900: +36% относительно 8000, но +9% относительно 10000 -> не срабатывает
    assert evaluate(
        sub_notified,
        price=10900,
        offer_id="o1",
        now=1000.0,
        recent_prices=[],
        reference_price=8000,
    ) == []

    # 11000: +10% относительно 10000 -> срабатывает относительно 10000
    res = evaluate(
        sub_notified,
        price=11000,
        offer_id="o1",
        now=1000.0,
        recent_prices=[],
        reference_price=8000,
    )
    assert res == ["подорожала на 1000 ₽ (10%)"]

    # Если last_notified_price=None, используется reference_price
    sub_new = _make_sub(last_notified_price=None)
    res_ref = evaluate(
        sub_new,
        price=11000,
        offer_id="o1",
        now=1000.0,
        recent_prices=[],
        reference_price=10000,
    )
    assert res_ref == ["подорожала на 1000 ₽ (10%)"]

    # Без reference_price и last_notified_price рост не срабатывает
    assert evaluate(
        sub_new,
        price=11000,
        offer_id="o1",
        now=1000.0,
        recent_prices=[],
        reference_price=None,
    ) == []


def test_pause_and_deduplication_suppress_repeat():
    """Пауза и дедупликация подавляют повторные уведомления о росте."""
    rules = NotifyRules(pause_seconds=3 * 3600.0)

    # 1. Пауза
    sub_paused = _make_sub(
        last_notified_price=10000,
        last_notified_at=1000.0,
        last_notified_offer_id="o1",
    )
    # Прошло 1 час (< 3 часов): уведомлений нет даже при дальнейшем росте цены
    assert evaluate(
        sub_paused,
        price=12000,
        offer_id="o2",
        now=1000.0 + 3600.0,
        recent_prices=[],
        rules=rules,
    ) == []

    # Прошло 3 часа (пауза истекла) -> уведомление проходит
    reasons = evaluate(
        sub_paused,
        price=12000,
        offer_id="o2",
        now=1000.0 + 3 * 3600.0,
        recent_prices=[],
        rules=rules,
    )
    assert reasons == ["подорожала на 2000 ₽ (20%)"]

    # 2. Дедупликация
    sub_dedup = _make_sub(
        last_notified_price=11000,
        last_notified_offer_id="off_dup",
        last_notified_at=1000.0,
    )
    # Пауза давно истекла (now = 50000.0), но offer_id и цена совпадают
    assert evaluate(
        sub_dedup,
        price=11000,
        offer_id="off_dup",
        now=50000.0,
        recent_prices=[],
        rules=rules,
    ) == []


def test_price_drop_unaffected_and_no_collision():
    """Логика падения не затронута, 'новый минимум' и рост не могут сработать вместе."""
    rules = NotifyRules()
    sub = _make_sub(min_price=10000, last_notified_price=10000)

    # Падение цены
    res_drop = evaluate(sub, price=9000, offer_id="o1", now=1000.0, recent_prices=[], rules=rules)
    assert "упала на 1000 ₽ (10%)" in res_drop
    assert "новый минимум" in res_drop
    assert not any(r.startswith("подорожала") for r in res_drop)

    # Рост цены: 'новый минимум' не может сработать вместе с ростом
    res_rise = evaluate(sub, price=11000, offer_id="o2", now=1000.0, recent_prices=[], rules=rules)
    assert res_rise == ["подорожала на 1000 ₽ (10%)"]
    assert "новый минимум" not in res_rise


def test_build_notification_for_rise_vs_drop_vs_mixed():
    """build_notification для роста содержит 'Цены дорожают' и подсказку, для падения остаётся 'Цена изменилась'."""
    sub = Subscription(
        id=10,
        chat_id=123,
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
        return_date=None,
        adults=1,
        baggage_mode="any",
        criteria=SearchCriteria(),
    )
    offer = Offer(
        site="aviasales",
        price=11000,
        currency="RUB",
        airline="Аэрофлот",
        depart_time="12:00",
        arrive_time="13:30",
        transfers=0,
        url="https://avia.ru/1",
        url_is_direct=True,
    )

    # 1. Уведомление о росте цены
    rise_reasons = ["подорожала на 1000 ₽ (10%)"]
    text_rise = build_notification(sub, offer, rise_reasons)
    lines_rise = text_rise.split("\n")

    assert lines_rise[0] == "📈 <b>Цены дорожают</b>"
    assert lines_rise[2] == "⚠️ Причина: подорожала на 1000 ₽ (10%)"
    assert "11 000 ₽" in lines_rise[3]
    # Подсказка отдельной строкой после карточки билета
    hint = "Если планируете лететь, возможно, стоит не откладывать покупку."
    assert hint in text_rise
    assert lines_rise[4] == hint
    assert "🔔 <b>Цена изменилась</b>" not in text_rise

    # 2. Уведомление о падении цены
    drop_reasons = ["новый минимум", "упала на 1000 ₽ (10%)"]
    text_drop = build_notification(sub, offer, drop_reasons)
    lines_drop = text_drop.split("\n")

    assert lines_drop[0] == "🔔 <b>Цена изменилась</b>"
    assert lines_drop[2] == "Причина: новый минимум; упала на 1000 ₽ (10%)"
    assert "📈 <b>Цены дорожают</b>" not in text_drop
    assert hint not in text_drop

    # 3. Смешанный случай (рост + порог) остаётся как сейчас
    mixed_reasons = ["подорожала на 1000 ₽ (10%)", "ниже вашего порога 15000 ₽"]
    text_mixed = build_notification(sub, offer, mixed_reasons)
    lines_mixed = text_mixed.split("\n")

    assert lines_mixed[0] == "🔔 <b>Цена изменилась</b>"
    assert lines_mixed[2] == "Причина: подорожала на 1000 ₽ (10%); ниже вашего порога 15000 ₽"
    assert "📈 <b>Цены дорожают</b>" not in text_mixed
    assert hint not in text_mixed


@pytest.mark.asyncio
async def test_scheduler_run_once_price_rise_end_to_end(store, clock, today):
    """Scheduler.run_once с подпиской, где первая цена 10000, а затем 11000, отправляет одно уведомление о росте."""
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

    sent_notifications: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent_notifications.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        clock=clock,
        today=today,
    )

    # 1. Первый проход: цена 10000 записывается как первая цена, уведомлений нет
    count1 = await sched.run_once()
    assert count1 == 0
    assert len(sent_notifications) == 0
    assert store.first_price(sub.id) == 10000

    # 2. Второй проход: цена выросла до 11000 (+10%)
    clock.advance(3600)
    current_price = 11000

    count2 = await sched.run_once()
    assert count2 == 1
    assert len(sent_notifications) == 1

    chat_id, text, markup = sent_notifications[0]
    assert chat_id == 101
    assert "📈 <b>Цены дорожают</b>" in text
    assert "⚠️ Причина: подорожала на 1000 ₽ (10%)" in text
    assert "Если планируете лететь, возможно, стоит не откладывать покупку." in text

    # Проверяем обновление подписки
    sub_after = store.get(sub.id)
    assert sub_after is not None
    assert sub_after.last_notified_price == 11000
    assert sub_after.last_notified_at == clock()

    # 3. Следующий проход сразу же (цена всё ещё 11000): пауза и дедупликация подавляют отправку
    count3 = await sched.run_once()
    assert count3 == 0
    assert len(sent_notifications) == 1

    # 4. Прошло больше 3 часов (пауза истекла), но цена всё ещё 11000:
    clock.advance(4 * 3600)
    count4 = await sched.run_once()
    assert count4 == 0
    assert len(sent_notifications) == 1

    # 5. Новый рост на 10% от нового эталона 11000 -> 12100:
    clock.advance(3600)
    current_price = 12100
    count5 = await sched.run_once()
    assert count5 == 1
    assert len(sent_notifications) == 2

    chat_id5, text5, _ = sent_notifications[1]
    assert "⚠️ Причина: подорожала на 1100 ₽ (10%)" in text5
