"""Тесты панели разработчика /admin, безопасности и аналитических методов."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import gzip
from html import escape
import os
from pathlib import Path
import sqlite3
import time

import pytest

from avia import admin_panel
from avia.bot import Bot
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.history import PriceHistory
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.subscriptions import SubscriptionStore
from avia.users import UserStore


class FakeBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.next_msg_id = 200

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        if method == "editMessageText":
            return {"message_id": params.get("message_id"), "chat": {"id": params.get("chat_id")}}
        return {"ok": True}


def make_bot(tmp_path, allowed_ids: set[int] | None = None, tg_open: bool = True) -> FakeBot:
    storage = tmp_path / "storage"
    backup = tmp_path / "backup"
    storage.mkdir(parents=True, exist_ok=True)
    backup.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="secret_token_123",
        tg_allowed_ids=allowed_ids or {1},
        tg_open=tg_open,
        storage_dir=storage,
        backup_dir=backup,
    )
    return FakeBot(settings)


# ---------------------------------------------------------------------------
# 1. Безопасность и разграничение доступа
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_access_command_allowed(tmp_path):
    """Администратор получает главную панель по /admin с inline-кнопками."""
    bot = make_bot(tmp_path, allowed_ids={1})
    msg = {
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "username": "admin_user"},
        "text": "/admin",
    }
    await bot.handle(msg)

    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "🛠 <b>Панель разработчика</b>" in send_calls[0]["text"]
    kb = send_calls[0]["reply_markup"]["inline_keyboard"]
    # 4 ряда по 2 кнопки
    assert len(kb) == 4
    for row in kb:
        assert len(row) == 2


@pytest.mark.asyncio
async def test_admin_command_silently_ignored_for_non_admin(tmp_path):
    """Не-администратор молча игнорируется при вызове /admin (как в open, так и в closed)."""
    # Open mode
    bot_open = make_bot(tmp_path / "open", allowed_ids={1}, tg_open=True)
    msg_stranger = {
        "chat": {"id": 999, "type": "private"},
        "from": {"id": 999, "username": "stranger"},
        "text": "/admin",
    }
    await bot_open.handle(msg_stranger)
    assert not any(m in ("sendMessage", "editMessageText") for m, _ in bot_open.calls)

    # Closed mode
    bot_closed = make_bot(tmp_path / "closed", allowed_ids={1}, tg_open=False)
    await bot_closed.handle(msg_stranger)
    assert not any(m in ("sendMessage", "editMessageText") for m, _ in bot_closed.calls)


@pytest.mark.asyncio
async def test_admin_callback_forged_rejected_without_data(tmp_path):
    """На подделанный adm:* callback не-администратор получает 'Нет доступа' и 0 данных."""
    bot = make_bot(tmp_path, allowed_ids={1}, tg_open=True)

    cq = {
        "id": "cq_hack_1",
        "from": {"id": 999, "username": "hacker"},
        "message": {"chat": {"id": 999}, "message_id": 42},
        "data": "adm:over",
    }
    await bot.handle_callback(cq)

    # Проверяем, что был ответ "Нет доступа"
    ans_calls = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans_calls) == 1
    assert ans_calls[0]["callback_query_id"] == "cq_hack_1"
    assert ans_calls[0].get("text") == "Нет доступа"

    # Критично: нет editMessageText/sendMessage с данными панели
    assert not any(m in ("sendMessage", "editMessageText") for m, _ in bot.calls)


@pytest.mark.asyncio
async def test_admin_callback_backup_now_rejected_for_non_admin(tmp_path):
    """Попытка вызвать adm:backup_now не-админом отклоняется без выполнения бэкапа."""
    bot = make_bot(tmp_path, allowed_ids={1}, tg_open=True)

    cq = {
        "id": "cq_hack_backup",
        "from": {"id": 999, "username": "hacker"},
        "message": {"chat": {"id": 999}, "message_id": 42},
        "data": "adm:backup_now",
    }
    await bot.handle_callback(cq)

    ans_calls = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans_calls) == 1
    assert ans_calls[0].get("text") == "Нет доступа"
    assert not any(m in ("sendMessage", "editMessageText") for m, _ in bot.calls)
    assert bot.last_backup_at is None


# ---------------------------------------------------------------------------
# 2. Построение разделов на пустых и заполненных данных
# ---------------------------------------------------------------------------


def test_build_home_keyboard_and_limits():
    """Главная клавиатура: кнопки по 2 в ряд, callback_data <= 64 байт."""
    kb = admin_panel.make_home_keyboard()
    rows = kb["inline_keyboard"]
    assert len(rows) == 4
    for row in rows:
        assert len(row) == 2
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_all_sections_empty_data():
    """Все разделы корректно строятся на пустых данных без сбоев, длина <= 3800."""
    ctx_over = {
        "uptime_seconds": 7200,
        "git_version": "—",
        "users_total": 0,
        "users_active_24h": 0,
        "users_active_7d": 0,
        "users_new_24h": 0,
        "searches_today": 0,
        "bg_searches": "выключен",
        "active_subs": 0,
        "busy_searches": 0,
        "last_backup_at": None,
        "now": time.time(),
        "state_size_mb": 0.0,
    }
    txt_over = admin_panel.build_overview(ctx_over)
    assert "📊 <b>Обзор</b>" in txt_over
    assert len(txt_over) <= 3800

    ctx_parse = {"summary": None, "counts_by_source": {}, "recent_errors": []}
    txt_parse = admin_panel.build_parse(ctx_parse, "24h")
    assert "Данных пока нет: метрики копятся с момента включения" in txt_parse

    ctx_users = {
        "total": 0,
        "blocked": 0,
        "new_24h": 0,
        "new_7d": 0,
        "active_24h": 0,
        "active_7d": 0,
        "searches": 0,
        "top_searchers": [],
        "recent_users": [],
    }
    txt_users = admin_panel.build_users(ctx_users)
    assert "👥 <b>Пользователи</b>" in txt_users
    assert len(txt_users) <= 3800

    ctx_db = {
        "db_files": [],
        "history_stats": {},
        "subscriptions_stats": {},
        "users_count": 0,
        "metrics_runs_count": 0,
        "history_growth_24h": 0,
        "growth_mb_day": 0.0,
        "disk_free_gb": 10.0,
    }
    txt_db = admin_panel.build_db(ctx_db)
    assert "🗄 <b>База</b>" in txt_db
    assert len(txt_db) <= 3800

    ctx_tickets = {
        "routes_count": 0,
        "searches_count": 0,
        "avg_days_to_departure": None,
        "popular_routes": [],
        "baggage": {},
        "site_share": [],
    }
    txt_tickets = admin_panel.build_tickets(ctx_tickets)
    assert "🎫 <b>Билеты</b>" in txt_tickets
    assert len(txt_tickets) <= 3800

    ctx_subs = {
        "active_count": 0,
        "groups_count": 0,
        "top_routes": [],
        "overdue": {"total": 0, "overdue": 0, "max_lag_minutes": 0},
        "reasons": {},
        "notifications_24h": 0,
    }
    txt_subs = admin_panel.build_subs(ctx_subs)
    assert "🔔 <b>Подписки</b>" in txt_subs
    assert len(txt_subs) <= 3800

    ctx_backups = {"keep": 14, "hour": 3, "backups": []}
    txt_backups = admin_panel.build_backups(ctx_backups)
    assert "Копий пока нет" in txt_backups
    assert len(txt_backups) <= 3800


def test_all_sections_filled_data():
    """Все разделы форматируют заполненные данные по дизайну бота."""
    ctx_over = {
        "uptime_seconds": 180000,
        "git_version": "abc1234",
        "users_total": 42,
        "users_active_24h": 5,
        "users_active_7d": 12,
        "users_new_24h": 1,
        "searches_today": 15,
        "bg_searches": "7 из 50",
        "active_subs": 8,
        "busy_searches": 2,
        "last_backup_at": time.time() - 7200,
        "now": time.time(),
        "state_size_mb": 4.5,
    }
    txt_over = admin_panel.build_overview(ctx_over)
    assert "Аптайм: 2 дн 2 ч" in txt_over
    assert "Версия: abc1234" in txt_over
    assert "Поисков сегодня: людей — 15, фоновых — 7 из 50" in txt_over
    assert "2 ч назад" in txt_over

    ctx_parse = {
        "summary": {
            "_total": {
                "runs": 40,
                "ok": 38,
                "partial": 1,
                "captcha": 0,
                "failed": 2,
                "avg_elapsed": 2.34,
                "avg_offers": 14.5,
            },
            "aeroflot": {
                "runs": 20,
                "ok": 20,
                "partial": 0,
                "captcha": 0,
                "failed": 0,
                "avg_elapsed": 1.8,
                "avg_offers": 20.0,
            },
        },
        "counts_by_source": {"user": 10, "sub": 5, "bg": 25},
        "recent_errors": [
            {"ts": 1700000000, "site": "pobeda", "error": "connection timeout & retry error"}
        ],
    }
    txt_parse = admin_panel.build_parse(ctx_parse, "24h")
    assert "Итого: 40 запусков · ок 95% · неполных 1 · капч 0 · ошибок 2 · ср. время 2.3 с · ср. вариантов 14.5" in txt_parse
    assert "aeroflot: 20 запусков · ок 100%" in txt_parse
    assert "Люди: 10 · Подписки: 5 · Фон: 25" in txt_parse
    assert "pobeda: connection timeout &amp; retry error" in txt_parse

    ctx_tickets = {
        "routes_count": 5,
        "searches_count": 30,
        "avg_days_to_departure": 14.2,
        "popular_routes": [
            {"origin": "MOW", "destination": "LED", "count": 12, "min_price": 2500}
        ],
        "baggage": {"with": 10, "without": 10, "unknown": 0, "total": 20},
        "site_share": [("aeroflot", 50, 20)],
    }
    txt_tickets = admin_panel.build_tickets(ctx_tickets)
    assert "Маршрутов: 5 · Замеров: 30" in txt_tickets
    assert "Ср. дней до вылета: 14.2 дн" in txt_tickets
    assert "Москва → Санкт-Петербург: 12 замеров (от 2 500 ₽)" in txt_tickets
    assert "С багажом 50% · Без багажа 50%" in txt_tickets
    assert "aeroflot: 50 записей · 20 раз мин. цена" in txt_tickets


# ---------------------------------------------------------------------------
# 3. Устойчивость к сбоям источников данных
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_section_resilience_on_database_error(tmp_path, monkeypatch):
    """Сбой источника данных не роняет панель: выводится '⚠️ данные недоступны', кнопки работают."""
    bot = make_bot(tmp_path, allowed_ids={1})

    def fail_table_stats(*args, **kwargs):
        raise RuntimeError("Database corruption simulation")

    monkeypatch.setattr(bot.history, "table_stats", fail_table_stats)

    cq = {
        "id": "cq_admin_db",
        "from": {"id": 1, "username": "admin"},
        "message": {"chat": {"id": 1}, "message_id": 55},
        "data": "adm:db",
    }
    await bot.handle_callback(cq)

    edit_calls = [p for m, p in bot.calls if m == "editMessageText"]
    assert len(edit_calls) == 1
    text = edit_calls[0]["text"]
    assert "🗄 <b>База</b>" in text
    assert "⚠️ данные недоступны" in text

    # Проверяем наличие клавиатуры (◀ Назад, 🔄 Обновить)
    kb = edit_calls[0]["reply_markup"]["inline_keyboard"]
    assert any(btn["callback_data"] == "adm:home" for row in kb for btn in row)


# ---------------------------------------------------------------------------
# 4. Переключатель периода в Парсинге
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parsing_period_switch(tmp_path):
    """Переключение adm:parse:24h / adm:parse:7d корректно меняет окно метрик."""
    bot = make_bot(tmp_path, allowed_ids={1})
    now = time.time()
    bot.metrics.record_run("user", "aeroflot", ok=True, partial=False, captcha=False, error=None, offers=10, elapsed=1.5, ts=now - 3600)
    # Запись 3-дневной давности (попадёт в 7d, не попадёт в 24h)
    bot.metrics.record_run("user", "pobeda", ok=True, partial=False, captcha=False, error=None, offers=5, elapsed=2.0, ts=now - 3 * 86400)

    # 1. 24h
    cq_24 = {
        "id": "cq_p24",
        "from": {"id": 1, "username": "admin"},
        "message": {"chat": {"id": 1}, "message_id": 10},
        "data": "adm:parse:24h",
    }
    await bot.handle_callback(cq_24)
    text_24 = [p for m, p in bot.calls if m == "editMessageText"][-1]["text"]
    assert "🌐 <b>Парсинг</b> (24 ч)" in text_24
    assert "aeroflot" in text_24
    assert "pobeda" not in text_24

    # 2. 7d
    cq_7d = {
        "id": "cq_p7d",
        "from": {"id": 1, "username": "admin"},
        "message": {"chat": {"id": 1}, "message_id": 10},
        "data": "adm:parse:7d",
    }
    await bot.handle_callback(cq_7d)
    text_7d = [p for m, p in bot.calls if m == "editMessageText"][-1]["text"]
    assert "🌐 <b>Парсинг</b> (7 дн)" in text_7d
    assert "aeroflot" in text_7d
    assert "pobeda" in text_7d


# ---------------------------------------------------------------------------
# 5. Ручной бэкап и защита от повторных нажатий
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_now_flow_and_double_click_protection(tmp_path):
    """Кнопка 'Сделать бэкап сейчас' создаёт копии, повтор < 30 с отклоняется."""
    bot = make_bot(tmp_path, allowed_ids={1})

    # Создаём файл базы для бэкапа
    db_file = bot.s.storage_dir / "test.sqlite"
    with sqlite3.connect(db_file) as c:
        c.executescript("CREATE TABLE t (x INT); INSERT INTO t VALUES (1);")

    # 1. Первый запуск бэкапа
    cq1 = {
        "id": "cq_bk1",
        "from": {"id": 1, "username": "admin"},
        "message": {"chat": {"id": 1}, "message_id": 20},
        "data": "adm:backup_now",
    }
    await bot.handle_callback(cq1)

    ans_calls = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans_calls) == 1
    assert "Готово: создано" in ans_calls[0]["text"]
    assert bot.last_backup_at is not None

    # Проверяем, что раздел обновился через editMessageText
    edits = [p for m, p in bot.calls if m == "editMessageText"]
    assert len(edits) == 1
    assert "🛟 <b>Бэкапы</b>" in edits[0]["text"]

    # 2. Повторный клик через 1 секунду (должен быть отклонён защитой от спама)
    bot.calls.clear()
    cq2 = {
        "id": "cq_bk2",
        "from": {"id": 1, "username": "admin"},
        "message": {"chat": {"id": 1}, "message_id": 20},
        "data": "adm:backup_now",
    }
    await bot.handle_callback(cq2)
    ans2 = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans2) == 1
    assert "Подождите" in ans2[0]["text"]
    # editMessageText НЕ вызывался повторно
    assert not any(m == "editMessageText" for m, _ in bot.calls)


# ---------------------------------------------------------------------------
# 6. Новые методы UserStore, PriceHistory, SubscriptionStore
# ---------------------------------------------------------------------------


def test_users_new_methods(tmp_path):
    """Методы count_since_created, count_active_since, top_searchers в UserStore."""
    store = UserStore(tmp_path / "u.sqlite")
    now = 1_000_000.0

    store.register(10, username="u10", now=now - 500)
    store.register(20, username="u20", now=now - 100)
    store.register(30, username="u30", now=now - 50)
    # Обновляем last_seen_at для user 20
    store.register(20, username="u20", now=now - 40)

    # Имитируем поиски
    store.try_use_search(10, 10, now=now - 100)
    store.try_use_search(10, 10, now=now - 90)
    store.try_use_search(20, 10, now=now - 10)

    assert store.count_since_created(now - 200) == 2
    assert store.count_active_since(now - 60) == 2

    top = store.top_searchers(limit=2)
    assert len(top) == 2
    assert top[0].tg_id == 10 and top[0].total_searches == 2
    assert top[1].tg_id == 20 and top[1].total_searches == 1


def test_history_new_methods(tmp_path):
    """Методы table_stats, rows_since, route_min_price, site_share, tickets_overview в PriceHistory."""
    hist = PriceHistory(tmp_path / "h.sqlite")
    now = 1_000_000.0

    q1 = Query("MOW", "AER", date(2026, 12, 10))
    # Добавляем результаты с багажом и без
    res1 = [
        SiteResult("aeroflot", True, offers=[Offer("aeroflot", 5000, "RUB", baggage="с багажом", flight_numbers=["SU1"], offer_id="o1")], elapsed=1.0),
        SiteResult("pobeda", True, offers=[Offer("pobeda", 4000, "RUB", baggage="без багажа", flight_numbers=["DP1"], offer_id="o2")], elapsed=1.0),
    ]
    hist.annotate(q1, res1, now=now - 1000)

    q2 = Query("MOW", "LED", date(2026, 12, 15))
    res2 = [
        SiteResult("s7", True, offers=[Offer("s7", 3000, "RUB", baggage=None, flight_numbers=["S71"], offer_id="o3")], elapsed=1.0),
    ]
    hist.annotate(q2, res2, now=now - 500)

    # table_stats
    st = hist.table_stats()
    assert st["price_history"] >= 3
    assert st["earliest_ts"] is not None and st["latest_ts"] is not None
    assert st["route_daily"] == 0
    assert st["scheduler_heartbeat"] == 0

    # rows_since
    assert hist.rows_since(now - 700) >= 1
    assert hist.rows_since(now + 100) == 0

    # route_min_price
    min_mow_aer = hist.route_min_price("MOW", "AER", days=7, now=now)
    assert min_mow_aer == 4000
    assert hist.route_min_price("KUF", "EVN", days=7, now=now) is None

    # site_share
    shares = hist.site_share(days=7, now=now)
    # pobeda выиграла у aeroflot на маршруте MOW-AER
    sites_dict = {s[0]: s for s in shares}
    assert "pobeda" in sites_dict
    assert sites_dict["pobeda"][2] >= 1  # число побед

    # tickets_overview
    ov = hist.tickets_overview(days=7, now=now)
    assert ov["routes_count"] == 2
    assert ov["searches_count"] >= 2
    assert ov["baggage_with"] >= 1
    assert ov["baggage_without"] >= 1


def test_subscriptions_new_methods(tmp_path):
    """Методы notifications_since, table_stats, route_stats в SubscriptionStore."""
    store = SubscriptionStore(tmp_path / "s.sqlite")
    now = 1_000_000.0

    q1 = Query("MOW", "AER", date(2026, 12, 1))
    sub1 = store.create(100, q1, "any", SearchCriteria(), now=now - 2000)
    store.record_price(sub1.id, 5000, "RUB", "o1", now - 1500)

    q2 = Query("MOW", "AER", date(2026, 12, 5))
    sub2 = store.create(200, q2, "any", SearchCriteria(), now=now - 1000)
    store.deactivate(sub2.id, 200)

    store.record_notification(sub1.id, "o1", 4500, ["новый минимум"], now=now - 500)
    store.record_notification(sub1.id, "o1", 4000, ["упала на 500 ₽ (11%)"], now=now - 100)

    # notifications_since
    assert store.notifications_since(now - 300) == 1
    assert store.notifications_since(now - 1000) == 2

    # table_stats
    st = store.table_stats()
    assert st["subscriptions"] == 2
    assert st["active_subscriptions"] == 1
    assert st["sub_prices"] == 1
    assert st["notifications"] == 2

    # route_stats
    r_st = store.route_stats(limit=5)
    assert r_st["active_count"] == 1
    assert r_st["groups_count"] == 1
    assert len(r_st["top_routes"]) == 1
    assert r_st["top_routes"][0] == ("MOW", "AER", 1)


# ---------------------------------------------------------------------------
# 7. Обработка ошибки 'not modified' от Telegram
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_message_not_modified_handled(tmp_path):
    """Ошибка Telegram 'message is not modified' перехватывается, callback отвечает штатно."""
    bot = make_bot(tmp_path, allowed_ids={1})

    async def fail_call(method: str, **params):
        if method == "editMessageText":
            raise RuntimeError("telegram editMessageText: {'ok': False, 'description': 'Bad Request: message is not modified'}")
        bot.calls.append((method, params))
        return {"ok": True}

    bot.call = fail_call

    cq = {
        "id": "cq_not_mod",
        "from": {"id": 1, "username": "admin"},
        "message": {"chat": {"id": 1}, "message_id": 88},
        "data": "adm:home",
    }
    # Не должно бросать исключений
    await bot.handle_callback(cq)

    ans = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans) == 1
    assert ans[0]["callback_query_id"] == "cq_not_mod"


def test_build_parse_telephones_and_site_device():
    """Раздел Парсинг корректно отображает блоки Телефоны и Сайт × телефон."""
    ctx = {
        "summary": {
            "_total": {"runs": 20, "ok": 18, "partial": 1, "captcha": 1, "failed": 2, "avg_elapsed": 3.0, "avg_offers": 10.0},
            "tutu": {"runs": 10, "ok": 9, "partial": 0, "captcha": 1, "failed": 1, "avg_elapsed": 3.5, "avg_offers": 12.0},
            "onetwotrip": {"runs": 10, "ok": 9, "partial": 1, "captcha": 0, "failed": 1, "avg_elapsed": 2.5, "avg_offers": 8.0},
        },
        "devices_summary": {
            "realme": {
                "runs": 12,
                "ok": 10,
                "partial": 1,
                "captcha": 1,
                "failed": 2,
                "avg_fetch": 3.2,
                "avg_queue_wait": 0.4,
            },
            "redmi": {
                "runs": 8,
                "ok": 8,
                "partial": 0,
                "captcha": 0,
                "failed": 0,
                "avg_fetch": 2.1,
                "avg_queue_wait": 0.1,
            },
        },
        "site_devices_summary": {
            ("tutu", "realme"): {
                "runs": 10,
                "ok": 9,
                "partial": 0,
                "captcha": 1,
                "failed": 1,
                "avg_fetch": 3.5,
                "avg_queue_wait": 0.4,
            },
            ("onetwotrip", "redmi"): {
                "runs": 8,
                "ok": 8,
                "partial": 0,
                "captcha": 0,
                "failed": 0,
                "avg_fetch": 2.1,
                "avg_queue_wait": 0.1,
            },
            ("onetwotrip", "realme"): {
                "runs": 2,  # < 3 запусков, не должно попасть в список
                "ok": 1,
                "partial": 1,
                "captcha": 0,
                "failed": 1,
                "avg_fetch": 2.0,
                "avg_queue_wait": 0.5,
            },
        },
        "counts_by_source": {"user": 15, "sub": 5, "bg": 0},
        "recent_errors": [],
    }

    txt = admin_panel.build_parse(ctx, "24h")
    assert "realme: 12 запусков · ок 83% · неполных 1 · капч 1 · ср. запрос 3.2 с · ср. ожидание 0.4 с" in txt
    assert "redmi: 8 запусков · ок 100% · неполных 0 · капч 0 · ср. запрос 2.1 с · ср. ожидание 0.1 с" in txt
    assert "tutu × realme: 10 запусков · ок 90% · неполных 0 · капч 1 · ср. запрос 3.5 с · ср. ожидание 0.4 с" in txt
    assert "onetwotrip × redmi: 8 запусков · ок 100% · неполных 0 · капч 0 · ср. запрос 2.1 с · ср. ожидание 0.1 с" in txt
    # Пара с 2 запусками не должна отображаться
    assert "onetwotrip × realme" not in txt
    assert len(txt) <= 3800
