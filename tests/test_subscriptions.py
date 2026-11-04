from __future__ import annotations

from datetime import date
import json

from avia.criteria import SearchCriteria
from avia.models import Query
from avia.subscriptions import (
    NotifyRules,
    Subscription,
    SubscriptionStore,
    criteria_from_json,
    criteria_to_json,
    evaluate,
    poll_interval_seconds,
)


def _query(origin: str = "MOW", destination: str = "LED", depart: date | None = None) -> Query:
    return Query(
        origin=origin,
        destination=destination,
        depart=depart or date(2026, 12, 1),
        return_date=None,
        adults=1,
    )


def test_criteria_json_roundtrip_and_garbage():
    # 1. Roundtrip with full criteria
    crit = SearchCriteria(
        max_transfers=1,
        max_duration_hours=12,
        depart_parts=frozenset(["evening", "morning"]),
        arrive_parts=frozenset(["night"]),
        hour_cost=500,
    )
    serialized = criteria_to_json(crit)
    data = json.loads(serialized)
    assert data["depart_parts"] == ["evening", "morning"]  # sorted list
    assert data["arrive_parts"] == ["night"]
    assert criteria_from_json(serialized) == crit

    # 2. Roundtrip with default criteria
    default_crit = SearchCriteria()
    assert criteria_from_json(criteria_to_json(default_crit)) == default_crit

    # 3. Garbage and unexpected formats return default SearchCriteria
    assert criteria_from_json("") == default_crit
    assert criteria_from_json("invalid json {") == default_crit
    assert criteria_from_json("123") == default_crit
    assert criteria_from_json("[]") == default_crit
    assert criteria_from_json(None) == default_crit  # type: ignore

    # 4. Unknown keys ignored, invalid field types sanitized
    partial = json.dumps({
        "unknown_key": "some_value",
        "max_transfers": 2,
        "max_duration_hours": "not_an_int",
        "depart_parts": ["day", 123, None],
    })
    parsed = criteria_from_json(partial)
    assert parsed.max_transfers == 2
    assert parsed.max_duration_hours is None
    assert parsed.depart_parts == frozenset(["day"])


def test_subscription_dataclass_methods():
    depart = date(2026, 11, 20)
    ret = date(2026, 11, 27)
    crit = SearchCriteria(max_transfers=0)
    sub = Subscription(
        id=1,
        chat_id=123,
        origin="MOW",
        destination="AER",
        depart=depart,
        return_date=ret,
        adults=2,
        baggage_mode="with_baggage",
        criteria=crit,
    )

    q = sub.query()
    assert q.origin == "MOW"
    assert q.destination == "AER"
    assert q.depart == depart
    assert q.return_date == ret
    assert q.adults == 2

    assert sub.group_key() == ("MOW", "AER", depart, ret, 2)


def test_create_deduplication_and_limit_via_count_active(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    chat_id = 42
    q1 = _query(origin="MOW", destination="LED")
    crit1 = SearchCriteria(max_transfers=1)

    # First creation
    sub1 = store.create(chat_id, q1, baggage_mode="any", criteria=crit1, now=100.0)
    assert sub1.id is not None
    assert store.count_active(chat_id) == 1

    # Duplicate creation with same query, baggage, criteria returns same subscription
    sub1_dup = store.create(chat_id, q1, baggage_mode="any", criteria=crit1, now=200.0)
    assert sub1_dup.id == sub1.id
    assert store.count_active(chat_id) == 1

    # Different criteria creates another subscription
    crit2 = SearchCriteria(max_transfers=0)
    sub2 = store.create(chat_id, q1, baggage_mode="any", criteria=crit2, now=300.0)
    assert sub2.id != sub1.id
    assert store.count_active(chat_id) == 2

    # Different baggage mode creates another
    sub3 = store.create(chat_id, q1, baggage_mode="with_baggage", criteria=crit1, now=400.0)
    assert sub3.id != sub1.id
    assert sub3.id != sub2.id
    assert store.count_active(chat_id) == 3

    # Limit check via count_active
    max_subs = 3
    assert store.count_active(chat_id) >= max_subs


def test_list_active_only_own_chat(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    chat_a = 100
    chat_b = 200

    q_a1 = _query(destination="LED")
    q_a2 = _query(destination="AER")
    q_b1 = _query(destination="OVB")

    sub_a1 = store.create(chat_a, q_a1, "any", SearchCriteria())
    sub_a2 = store.create(chat_a, q_a2, "any", SearchCriteria())
    sub_b1 = store.create(chat_b, q_b1, "any", SearchCriteria())

    # Chat A sees only its 2 active subscriptions
    active_a = store.list_active(chat_a)
    assert {s.id for s in active_a} == {sub_a1.id, sub_a2.id}

    # Chat B sees only its 1 active subscription
    active_b = store.list_active(chat_b)
    assert {s.id for s in active_b} == {sub_b1.id}

    # Deactivating one of Chat A's subscriptions removes it from list_active
    assert store.deactivate(sub_a1.id, chat_a) is True
    assert {s.id for s in store.list_active(chat_a)} == {sub_a2.id}


def test_deactivate_foreign_subscription_fails(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    sub = store.create(100, _query(), "any", SearchCriteria())

    # Another chat cannot deactivate someone else's subscription
    assert store.deactivate(sub.id, chat_id=999) is False
    assert store.get(sub.id).active is True

    # Owner can deactivate
    assert store.deactivate(sub.id, chat_id=100) is True
    assert store.get(sub.id).active is False

    # Deactivating already deactivated subscription returns False
    assert store.deactivate(sub.id, chat_id=100) is False


def test_set_threshold(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    sub = store.create(100, _query(), "any", SearchCriteria())
    assert sub.threshold is None

    # Foreign chat cannot set threshold
    assert store.set_threshold(sub.id, chat_id=999, price=8000) is False
    assert store.get(sub.id).threshold is None

    # Non-existent subscription returns False
    assert store.set_threshold(9999, chat_id=100, price=8000) is False

    # Owner sets threshold
    assert store.set_threshold(sub.id, chat_id=100, price=8500) is True
    assert store.get(sub.id).threshold == 8500

    # Owner resets threshold to None
    assert store.set_threshold(sub.id, chat_id=100, price=None) is True
    assert store.get(sub.id).threshold is None


def test_expire_past(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    today = date(2026, 10, 15)

    sub_past = store.create(1, _query(depart=date(2026, 10, 10)), "any", SearchCriteria())
    sub_today = store.create(1, _query(depart=date(2026, 10, 15)), "any", SearchCriteria())
    sub_future = store.create(1, _query(depart=date(2026, 10, 20)), "any", SearchCriteria())

    # expire_past deactivates depart < today
    count = store.expire_past(today)
    assert count == 1

    assert store.get(sub_past.id).active is False
    assert store.get(sub_today.id).active is True
    assert store.get(sub_future.id).active is True

    # Re-running returns 0 because sub_past is already inactive
    assert store.expire_past(today) == 0


def test_poll_interval_seconds():
    today = date(2026, 10, 1)

    # > 30 days: 3600
    assert poll_interval_seconds(date(2026, 11, 1), today) == 3600  # 31 days
    assert poll_interval_seconds(date(2026, 12, 1), today) == 3600  # 61 days

    # 8 to 30 days: 2700
    assert poll_interval_seconds(date(2026, 10, 31), today) == 2700  # 30 days
    assert poll_interval_seconds(date(2026, 10, 9), today) == 2700   # 8 days

    # <= 7 days: 1800
    assert poll_interval_seconds(date(2026, 10, 8), today) == 1800   # 7 days
    assert poll_interval_seconds(date(2026, 10, 2), today) == 1800   # 1 day
    assert poll_interval_seconds(date(2026, 10, 1), today) == 1800   # 0 days
    assert poll_interval_seconds(date(2026, 9, 30), today) == 1800   # -1 days


def test_due_logic(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    today = date(2026, 10, 1)
    now = 10000.0

    def _interval(sub_id: int, base: int) -> float:
        j = ((sub_id * 37) % 21 - 10) / 100.0
        return base * (1.0 + j)

    # 1. Первый опрос: last_checked_at is None -> due
    sub_new = store.create(1, _query(depart=date(2026, 11, 15)), "any", SearchCriteria())

    # 2. Диапазон > 30 дней (interval = 3600 * (1 + j)):
    sub_long_due = store.create(1, _query(depart=date(2026, 11, 20)), "any", SearchCriteria())
    store.mark_checked(sub_long_due.id, now=now - _interval(sub_long_due.id, 3600))

    sub_long_not_due = store.create(1, _query(depart=date(2026, 11, 25)), "any", SearchCriteria())
    store.mark_checked(sub_long_not_due.id, now=now - _interval(sub_long_not_due.id, 3600) + 10)

    # 3. Диапазон 8-30 дней (interval = 2700 * (1 + j)):
    sub_mid_due = store.create(1, _query(depart=date(2026, 10, 20)), "any", SearchCriteria())
    store.mark_checked(sub_mid_due.id, now=now - _interval(sub_mid_due.id, 2700))

    sub_mid_not_due = store.create(1, _query(depart=date(2026, 10, 25)), "any", SearchCriteria())
    store.mark_checked(sub_mid_not_due.id, now=now - _interval(sub_mid_not_due.id, 2700) + 10)

    # 4. Диапазон <= 7 дней (interval = 1800 * (1 + j)):
    sub_short_due = store.create(1, _query(depart=date(2026, 10, 5)), "any", SearchCriteria())
    store.mark_checked(sub_short_due.id, now=now - _interval(sub_short_due.id, 1800))

    sub_short_not_due = store.create(1, _query(depart=date(2026, 10, 6)), "any", SearchCriteria())
    store.mark_checked(sub_short_not_due.id, now=now - _interval(sub_short_not_due.id, 1800) + 10)

    # 5. Неактивные подписки не попадают
    sub_inactive = store.create(1, _query(depart=date(2026, 10, 2)), "any", SearchCriteria())
    store.deactivate(sub_inactive.id, chat_id=1)

    due_list = store.due(now=now, today=today)
    due_ids = {s.id for s in due_list}

    assert sub_new.id in due_ids
    assert sub_long_due.id in due_ids
    assert sub_mid_due.id in due_ids
    assert sub_short_due.id in due_ids

    assert sub_long_not_due.id not in due_ids
    assert sub_mid_not_due.id not in due_ids
    assert sub_short_not_due.id not in due_ids
    assert sub_inactive.id not in due_ids


def test_due_deterministic_jitter(tmp_path):
    """Детерминированный разброс due(): диапазон ±10%, стабилен между запусками, новая проверяется сразу."""
    store = SubscriptionStore(tmp_path / "subs.db")
    today = date(2026, 10, 1)
    now = 10000.0

    # Создаём несколько подписок на одну дату (базовый интервал 3600) у разных пользователей
    subs = [store.create(i, _query(depart=date(2026, 11, 20)), "any", SearchCriteria()) for i in range(1, 11)]

    for sub in subs:
        j = ((sub.id * 37) % 21 - 10) / 100.0
        assert -0.10 <= j <= 0.10

    # Новые подписки без last_checked_at созревают сразу
    due_new = store.due(now=now, today=today)
    assert len(due_new) == 10

    # Отмечаем все проверенными ровно 3600 с назад (базовый интервал)
    for sub in subs:
        store.mark_checked(sub.id, now=now - 3600)

    due_jittered = store.due(now=now, today=today)
    due_jittered_2 = store.due(now=now, today=today)
    # Детерминированность: повторный вызов даёт ровно те же подписки
    assert [s.id for s in due_jittered] == [s.id for s in due_jittered_2]

    # Не все созрели одновременно (разброс сработал: часть с j<=0 созрели, с j>0 — нет)
    due_ids = {s.id for s in due_jittered}
    for sub in subs:
        j = ((sub.id * 37) % 21 - 10) / 100.0
        expected_interval = 3600 * (1.0 + j)
        if 3600 >= expected_interval:
            assert sub.id in due_ids
        else:
            assert sub.id not in due_ids


def test_record_price_updates_min_and_history(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    sub = store.create(1, _query(), "any", SearchCriteria())
    assert sub.min_price is None

    # First price recorded
    store.record_price(sub.id, price=10000, currency="RUB", offer_id="off1", now=100.0)
    sub = store.get(sub.id)
    assert sub.min_price == 10000
    assert store.first_price(sub.id) == 10000

    # Lower price updates min_price
    store.record_price(sub.id, price=7500, currency="RUB", offer_id="off2", now=200.0)
    sub = store.get(sub.id)
    assert sub.min_price == 7500
    assert store.first_price(sub.id) == 10000

    # Higher price does not overwrite min_price
    store.record_price(sub.id, price=8500, currency="RUB", offer_id="off3", now=300.0)
    sub = store.get(sub.id)
    assert sub.min_price == 7500
    assert store.first_price(sub.id) == 10000

    # Recent prices in time window
    assert store.recent_prices(sub.id, since=150.0) == [7500, 8500]
    assert store.recent_prices(sub.id, since=50.0) == [10000, 7500, 8500]


def test_record_notification(tmp_path):
    store = SubscriptionStore(tmp_path / "subs.db")
    sub = store.create(1, _query(), "any", SearchCriteria())

    store.record_notification(
        sub_id=sub.id,
        offer_id="off99",
        price=6000,
        reasons=["новый минимум", "ниже вашего порога 7000 ₽"],
        now=500.0,
    )
    updated = store.get(sub.id)
    assert updated.last_notified_at == 500.0
    assert updated.last_notified_price == 6000
    assert updated.last_notified_offer_id == "off99"


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


def test_evaluate_reasons_individually():
    rules = NotifyRules()

    # 1. Новый минимум
    sub1 = _make_sub(min_price=10000)
    assert evaluate(sub1, price=9500, offer_id="o1", now=1000.0, recent_prices=[], rules=rules) == ["новый минимум"]
    assert evaluate(sub1, price=10000, offer_id="o1", now=1000.0, recent_prices=[], rules=rules) == []

    # 2. Упала на N ₽ (P%) по drop_rub
    sub2 = _make_sub(last_notified_price=10000)
    # падение на 500 руб (5.0%)
    res2 = evaluate(sub2, price=9500, offer_id="o2", now=1000.0, recent_prices=[], rules=rules)
    assert res2 == ["упала на 500 ₽ (5%)"]

    # Упала по drop_pct (падение < 500 руб, но >= 5%)
    sub2_pct = _make_sub(last_notified_price=2000)
    # падение на 150 руб = 7.5% -> round 8%
    res2_pct = evaluate(sub2_pct, price=1850, offer_id="o2", now=1000.0, recent_prices=[], rules=rules)
    assert res2_pct == ["упала на 150 ₽ (8%)"]

    # 3. Ниже порога
    sub3 = _make_sub(threshold=7000)
    assert evaluate(sub3, price=6500, offer_id="o3", now=1000.0, recent_prices=[], rules=rules) == ["ниже вашего порога 7000 ₽"]
    assert evaluate(sub3, price=7000, offer_id="o3", now=1000.0, recent_prices=[], rules=rules) == ["ниже вашего порога 7000 ₽"]
    assert evaluate(sub3, price=7001, offer_id="o3", now=1000.0, recent_prices=[], rules=rules) == []

    # 4. На N% ниже обычного
    sub4 = _make_sub()
    recent = [10000, 10000, 10000, 10000, 10000]  # медиана 10000
    # цена 8000 -> на 20% ниже медианы (>= 15%)
    res4 = evaluate(sub4, price=8000, offer_id="o4", now=1000.0, recent_prices=recent, rules=rules)
    assert res4 == ["на 20% ниже обычного"]


def test_evaluate_combination_of_reasons():
    # Все 4 причины одновременно:
    # - Новый минимум (price 7000 < min_price 9000)
    # - Упала относительно эталона (price 7000 < last_notified 10000, падение 3000 = 30%)
    # - Ниже порога (price 7000 <= threshold 8000)
    # - Ниже обычного (recent median 10000, 7000 на 30% ниже)
    sub = _make_sub(
        min_price=9000,
        last_notified_price=10000,
        last_notified_offer_id="old_off",
        threshold=8000,
    )
    recent = [10000, 10000, 10000, 10000, 10000]
    reasons = evaluate(sub, price=7000, offer_id="new_off", now=100000.0, recent_prices=recent)
    assert reasons == [
        "новый минимум",
        "упала на 3000 ₽ (30%)",
        "ниже вашего порога 8000 ₽",
        "на 30% ниже обычного",
    ]


def test_evaluate_pause():
    rules = NotifyRules(pause_seconds=3600.0)
    sub = _make_sub(min_price=10000, last_notified_at=1000.0)

    # Прошло 1000 секунд < 3600 -> пауза активна, уведомлений нет
    assert evaluate(sub, price=8000, offer_id="o1", now=2000.0, recent_prices=[], rules=rules) == []

    # Прошло 3600 секунд -> пауза истекла, уведомление проходит
    reasons = evaluate(sub, price=8000, offer_id="o1", now=4600.0, recent_prices=[], rules=rules)
    assert reasons == ["новый минимум"]


def test_evaluate_deduplication():
    # Пауза истекла, но offer_id и price те же самые
    sub = _make_sub(
        last_notified_at=1000.0,
        last_notified_price=8000,
        last_notified_offer_id="o1",
        min_price=9000,
    )
    # offer_id and price match -> dedup returns []
    assert evaluate(sub, price=8000, offer_id="o1", now=20000.0, recent_prices=[]) == []

    # Same price but different offer_id -> not duplicate
    reasons = evaluate(sub, price=8000, offer_id="o2", now=20000.0, recent_prices=[])
    assert "новый минимум" in reasons


def test_evaluate_no_reasons():
    sub = _make_sub(min_price=5000, last_notified_price=6000, threshold=4000)
    recent = [6000, 6000, 6000, 6000, 6000]
    # Цена 6500: не новый минимум, не упала, не ниже порога, не ниже медианы
    assert evaluate(sub, price=6500, offer_id="o1", now=1000.0, recent_prices=recent) == []


def test_evaluate_reference_price_without_notifications():
    # Уведомлений ещё не было, эталон берётся из reference_price
    sub = _make_sub(last_notified_price=None)

    # С reference_price = 10000, цена 9000 (падение 1000 ₽, 10%)
    res = evaluate(sub, price=9000, offer_id="o1", now=1000.0, recent_prices=[], reference_price=10000)
    assert res == ["упала на 1000 ₽ (10%)"]

    # Без reference_price и без прошлых уведомлений правило падения не срабатывает
    assert evaluate(sub, price=9000, offer_id="o1", now=1000.0, recent_prices=[], reference_price=None) == []


def test_evaluate_median_insufficient_points():
    sub = _make_sub()
    # Меньше median_min_points (4 точки при минимуме 5)
    recent = [10000, 10000, 10000, 10000]
    assert evaluate(sub, price=5000, offer_id="o1", now=1000.0, recent_prices=recent) == []
