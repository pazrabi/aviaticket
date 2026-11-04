"""Тесты расширенной команды /stats и новых методов SubscriptionStore."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import time

import pytest

from avia.bot import Bot
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult
from avia.subscriptions import SubscriptionStore


class FakeBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.next_msg_id = 100

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        return {"ok": True}


def make_bot(tmp_path, allowed_ids: set[int] | None = None) -> FakeBot:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="secret_token_123",
        tg_allowed_ids=allowed_ids or {1},
        storage_dir=storage,
    )
    return FakeBot(settings)


def test_notification_reason_counts_grouping_and_types(tmp_path):
    """notification_reason_counts группирует причины по 5 типам и фильтрует по since_ts."""
    store = SubscriptionStore(tmp_path / "subs.sqlite")
    q = Query("KUF", "EVN", date(2026, 12, 1))
    sub = store.create(100, q, "any", SearchCriteria())

    now_ts = 1000000.0

    # 1. Уведомление до since_ts (должно игнорироваться)
    store.record_notification(sub.id, "o0", 9000, ["новый минимум"], now=now_ts - 1000)

    # 2. Уведомления внутри окна since_ts
    since_ts = now_ts - 500

    # новый минимум и упала
    store.record_notification(
        sub.id,
        "o1",
        8500,
        ["новый минимум", "упала на 500 ₽ (5%)"],
        now=now_ts - 400,
    )

    # подорожала
    store.record_notification(
        sub.id,
        "o2",
        9500,
        ["подорожала на 1000 ₽ (10%)"],
        now=now_ts - 300,
    )

    # ниже порога
    store.record_notification(
        sub.id,
        "o3",
        7000,
        ["ниже вашего порога 7500 ₽"],
        now=now_ts - 200,
    )

    # ниже обычного
    store.record_notification(
        sub.id,
        "o4",
        6000,
        ["на 20% ниже обычного"],
        now=now_ts - 100,
    )

    # Текстовый формат через разделитель (на случай старых записей)
    with store._connect() as db:
        db.execute(
            "INSERT INTO notifications (sub_id, offer_id, price, reasons, sent_at) VALUES (?, ?, ?, ?, ?)",
            (sub.id, "o5", 5500, "новый минимум; упала на 500 ₽ (8%)", now_ts - 50),
        )

    counts = store.notification_reason_counts(since_ts)

    assert counts.get("новый минимум") == 2
    assert counts.get("упала") == 2
    assert counts.get("подорожала") == 1
    assert counts.get("ниже порога") == 1
    assert counts.get("ниже обычного") == 1


def test_overdue_stats_calculation(tmp_path):
    """overdue_stats считает активные подписки, просрочку и макс. отставание."""
    store = SubscriptionStore(tmp_path / "subs.sqlite")
    today = date(2026, 11, 1)
    now = 1000000.0

    # Sub 1: depart через 40 дней -> interval 3600 с.
    # Проверена 3600 + 42 * 60 с назад -> отставание ровно 42 мин.
    q1 = Query("KUF", "EVN", date(2026, 12, 11))
    sub1 = store.create(100, q1, "any", SearchCriteria(), now=now - 100000)
    store.mark_checked(sub1.id, now=now - 3600 - 42 * 60)

    # Sub 2: depart через 5 дней -> interval 1800 с.
    # Проверена 1000 с назад -> не просрочена.
    q2 = Query("MOW", "LED", date(2026, 11, 6))
    sub2 = store.create(100, q2, "any", SearchCriteria(), now=now - 50000)
    store.mark_checked(sub2.id, now=now - 1000)

    # Sub 3: depart через 15 дней -> interval 2700 с.
    # Проверена 2000 с назад -> не просрочена.
    q3 = Query("AER", "SVX", date(2026, 11, 16))
    sub3 = store.create(100, q3, "any", SearchCriteria(), now=now - 30000)
    store.mark_checked(sub3.id, now=now - 2000)

    # Sub 4: неактивная подписка -> должна игнорироваться
    q4 = Query("OVB", "IKT", date(2026, 12, 1))
    sub4 = store.create(100, q4, "any", SearchCriteria())
    store.deactivate(sub4.id, 100)

    stats = store.overdue_stats(now, today)
    assert stats["total"] == 3
    assert stats["overdue"] == 1
    assert stats["max_lag_minutes"] == 42


@pytest.mark.asyncio
async def test_admin_stats_full_output(tmp_path):
    """Команда /stats выводит существующую сводку и все 4 новых аналитических блока."""
    bot = make_bot(tmp_path, allowed_ids={1})
    now_ts = time.time()
    today_d = date.today()

    # 1. Добавляем данные пользователей
    bot.users.register(1, username="admin_user")
    bot.users.register(2, username="user_two")

    # 2. Популярные маршруты в history
    q = Query("KUF", "EVN", today_d + timedelta(days=40))
    for i in range(12):
        bot.history.annotate(
            q,
            [SiteResult("aviasales", True, [Offer("aviasales", price=9000, offer_id=f"off_{i}")])],
            now=now_ts - 3600 * (i + 1),
            search_id=f"search_{i}",
        )

    # 3. Heartbeats для coverage Мака
    # Записываем heartbeats за сегодня
    now_dt = datetime.fromtimestamp(now_ts)
    start_of_day = datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()
    bot.history.record_heartbeat(now=start_of_day + 100)
    bot.history.record_heartbeat(now=now_ts)

    # 4. Подписки и очередь проверок
    sub1 = bot.subs.create(100, q, "any", SearchCriteria(), now=now_ts - 200000)
    bot.subs.mark_checked(sub1.id, now=now_ts - 3600 - 42 * 60)

    # 5. Уведомления по причинам
    bot.subs.record_notification(sub1.id, "o1", 8500, ["новый минимум", "упала на 500 ₽ (5%)"], now=now_ts - 1000)

    msg = {
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "username": "admin_user"},
        "text": "/stats",
    }
    await bot.handle(msg)

    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    text = send_calls[0]["text"]

    # Проверяем базовые строки
    assert "Людей:" in text
    assert "Поисков сегодня:" in text
    assert "Последние:" in text

    # Блок 1: Популярные маршруты
    assert "Популярные маршруты (7 дней):" in text
    assert "Самара → Ереван: 12" in text

    # Блок 2: Уведомления по причинам
    assert "Уведомления по причинам (7 дней):" in text
    assert "новый минимум: 1" in text
    assert "упала: 1" in text

    # Блок 3: Очередь проверок
    assert "Очередь проверок:" in text
    assert "Подписок 1, просрочено 1, максимум отстаёт на 42 мин" in text

    # Блок 4: Мак работал сегодня
    assert "Мак работал сегодня:" in text
    assert "Работал" in text and "пропусков" in text


@pytest.mark.asyncio
async def test_admin_stats_error_resilience(tmp_path, monkeypatch):
    """Сбой в одном блоке не ломает /stats (все остальные блоки выводятся)."""
    bot = make_bot(tmp_path, allowed_ids={1})
    bot.users.register(1, username="admin_user")

    def broken_routes(*args, **kwargs):
        raise RuntimeError("popular routes DB error")

    monkeypatch.setattr(bot.history, "popular_routes", broken_routes)

    msg = {
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "username": "admin_user"},
        "text": "/stats",
    }
    await bot.handle(msg)

    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    text = send_calls[0]["text"]

    # Базовые строки и остальные блоки на месте
    assert "Людей:" in text
    assert "Очередь проверок:" in text
    assert "Мак работал сегодня:" in text
    # Сбойный блок не выведен, но команда не упала
    assert "Популярные маршруты" not in text


@pytest.mark.asyncio
async def test_admin_stats_bg_collect_disabled(tmp_path):
    """При bg_collect=False в /stats выводится 'Фоновый сбор: выключен'."""
    bot = make_bot(tmp_path, allowed_ids={1})
    bot.users.register(1, username="admin_user")

    msg = {
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "username": "admin_user"},
        "text": "/stats",
    }
    await bot.handle(msg)

    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "Фоновый сбор: выключен" in send_calls[0]["text"]


@pytest.mark.asyncio
async def test_admin_stats_bg_collect_enabled(tmp_path):
    """При bg_collect=True в /stats выводится 'Фоновый сбор: сегодня N из M'."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="secret_token_123",
        tg_allowed_ids={1},
        storage_dir=storage,
        bg_collect=True,
        bg_collect_max_per_day=50,
    )
    bot = FakeBot(settings)
    bot.users.register(1, username="admin_user")
    assert bot.bg_collector is not None
    bot.bg_collector._done_today = 7

    msg = {
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "username": "admin_user"},
        "text": "/stats",
    }
    await bot.handle(msg)

    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "Фоновый сбор: сегодня 7 из 50" in send_calls[0]["text"]

