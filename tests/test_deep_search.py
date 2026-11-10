"""Тесты глубокого поиска.

Проверяют:
(а) DeepCache: put/get, ttl, вытеснение при > 100;
(б) pipeline.search(deep=True) в _from_browser не передаёт evaluate;
(в) обработчик кнопки «🔬 Глубокий поиск»:
    - без snapshot -> «Сначала запустите обычный поиск»;
    - с snapshot -> вызывает search(deep=True), метрика cls='deep',
      отчёт с первой строкой «🔬 Глубокий поиск: полная выдача»;
    - повторное нажатие в течение ttl берёт кеш, не вызывает search и не списывает лимит;
(г) миграция searches добавляет cls в старую базу.
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from avia.bot import Bot
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.deep_cache import DeepCache
from avia.dialog import DeepSearchRequest, Dialog, Reply, SearchRequest, Session, Step
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.pipeline import search
from avia.store import ResultStore


# (а) Тесты DeepCache: put/get, ttl, вытеснение при >100
def test_deep_cache_put_get():
    """DeepCache сохраняет результаты и возвращает их с возрастом в секундах."""
    cache = DeepCache(ttl_seconds=900, max_entries=100)
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), return_date=None, adults=1)
    results = [SiteResult(site="test_site", ok=True, offers=[Offer(site="test_site", price=5000)])]

    cache.put(q, results, now=1000.0)

    # Запрос по Query
    cached = cache.get(q, now=1120.0)
    assert cached is not None
    got_results, age = cached
    assert len(got_results) == 1
    assert got_results[0].offers[0].price == 5000
    assert age == 120.0

    # Запрос по ключу-кортежу
    key_tuple = ("MOW", "LED", date(2026, 12, 10), None, 1)
    cached_tuple = cache.get(key_tuple, now=1150.0)
    assert cached_tuple is not None
    assert cached_tuple[1] == 150.0


def test_deep_cache_ttl():
    """DeepCache возвращает None, если запись старше ttl_seconds."""
    cache = DeepCache(ttl_seconds=900)
    q = Query(origin="MOW", destination="AER", depart=date(2026, 12, 15))
    results = [SiteResult(site="s", ok=True, offers=[])]

    cache.put(q, results, now=1000.0)

    # На границе TTL
    assert cache.get(q, now=1900.0) is not None
    # Старше TTL
    assert cache.get(q, now=1901.0) is None
    # После удаления запись отсутствует
    assert cache.get(q, now=1950.0) is None


def test_deep_cache_eviction_over_100():
    """DeepCache хранит не более 100 записей и вытесняет старейшие."""
    cache = DeepCache(ttl_seconds=900, max_entries=100)
    results = [SiteResult(site="s", ok=True, offers=[])]

    # Заполняем ровно 100 записей
    for i in range(100):
        q = Query(origin=f"A{i:02d}", destination="LED", depart=date(2026, 12, 10))
        cache.put(q, results, now=1000.0 + i)

    assert len(cache) == 100
    # Самая первая запись на месте
    first_q = Query(origin="A00", destination="LED", depart=date(2026, 12, 10))
    assert cache.get(first_q, now=1100.0) is not None

    # Добавляем 101-ю запись
    new_q = Query(origin="NEW", destination="LED", depart=date(2026, 12, 10))
    cache.put(new_q, results, now=1200.0)

    assert len(cache) == 100
    # Проверяем, что самая старая неиспользованная запись вытеснена
    second_q = Query(origin="A01", destination="LED", depart=date(2026, 12, 10))
    assert cache.get(second_q, now=1200.0) is None
    # Новая запись присутствует
    assert cache.get(new_q, now=1200.0) is not None


# (б) pipeline.search(deep=True) в _from_browser не передаёт evaluate
@pytest.mark.asyncio
async def test_pipeline_search_deep_does_not_pass_evaluate(monkeypatch):
    """pipeline.search(deep=True) не передаёт evaluate в fetch_page_snapshot_any."""
    site = SiteConfig(
        name="test_browser",
        url_template="https://example.com/{origin}/{destination}/{date_iso}",
        source="browser",
        phone="phone1",
        enabled=True,
    )
    settings = Settings(sites=[site], tg_token="test_token", tg_allowed_ids={1})

    calls: list[dict] = []

    async def fake_fetch_page_snapshot_any(st, cand, url, **kwargs):
        calls.append(kwargs)
        return Snapshot(url=url, text="", json_payloads=[]), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)
    monkeypatch.setattr("avia.pipeline.phone_enabled", lambda s, sc: True)
    monkeypatch.setattr("avia.pipeline.candidates", lambda s, sc, priority=0: ["phone1"])

    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))

    # 1. Обычный поиск (deep=False) — evaluate передаётся для правила остановки
    calls.clear()
    await search(q, settings, deep=False)
    assert len(calls) == 1
    assert "evaluate" in calls[0]
    assert calls[0]["evaluate"] is not None
    assert calls[0]["priority"] == 0

    # 2. Глубокий поиск (deep=True) — evaluate НЕ передаётся
    calls.clear()
    await search(q, settings, deep=True)
    assert len(calls) == 1
    assert "evaluate" not in calls[0]
    assert calls[0]["priority"] == 0


# (в) Тесты обработчика кнопки «🔬 Глубокий поиск»
class DeepFakeBot(Bot):
    """Тестовый бот для перехвата вызовов API Telegram."""

    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.next_msg_id = 500

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        if method == "answerCallbackQuery":
            return True
        if method == "deleteMessage":
            return True
        if method == "editMessageText":
            return True
        return {"ok": True}


@pytest.mark.asyncio
async def test_button_handler_without_snapshot(tmp_path):
    """Нажатие кнопки глубокого поиска без предыдущего быстрого поиска возвращает предупреждение."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(sites=[], tg_token="dummy_test_token_123", tg_allowed_ids={123}, storage_dir=storage)
    bot = DeepFakeBot(settings)

    # 1. Прямой вызов диалога без snapshot
    replies = bot.dialog.on_callback(123, "deep:none_dialog")
    assert len(replies) == 1
    assert isinstance(replies[0], Reply)
    assert replies[0].text == "Сначала запустите обычный поиск"

    # 2. Вызов через handle_callback бота
    cq = {
        "id": "cq_1",
        "from": {"id": 123},
        "data": "deep:none_dialog",
        "message": {"chat": {"id": 123}, "message_id": 10},
    }
    await bot.handle_callback(cq)

    # Бот ответил пользователю сообщением
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert any("Сначала запустите обычный поиск" in p.get("text", "") for p in send_calls)


@pytest.mark.asyncio
async def test_button_handler_with_snapshot_and_cache(tmp_path, monkeypatch):
    """Нажатие кнопки со snapshot вызывает search(deep=True), пишет метрику cls='deep',

    а повторное нажатие в пределах TTL отдаёт результат из кеша без вызова search и без списания лимита.
    """
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[SiteConfig(name="site1", url_template="http://test/{origin}/{destination}/{date_iso}", enabled=True)],
        tg_token="dummy_test_token_123",
        tg_allowed_ids=set(),  # обычный пользователь для проверки дневного лимита
        daily_search_limit=40,
        tg_open=True,
        storage_dir=storage,
    )
    bot = DeepFakeBot(settings)
    chat_id = 777
    bot.users.register(chat_id, "test_user", "Tester")

    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    quick_offers = [Offer(site="site1", price=6000, duration="1 ч 30 м", offer_id="id_quick")]
    deep_offers = [Offer(site="site1", price=4500, duration="1 ч 20 м", offer_id="id_deep")]

    # Сохраняем snapshot быстрого поиска
    bot.store.put(
        chat_id,
        q,
        [SiteResult("site1", True, quick_offers)],
        "any",
        quick_offers,
    )
    d_id = "test_dialog"
    bot.dialog.sessions[chat_id] = Session(step=Step.DONE, dialog_id=d_id, origin="MOW", destination="LED", depart=date(2026, 12, 10))

    search_calls: list[dict] = []

    async def mock_search(query, s, **kwargs):
        search_calls.append({"query": query, "deep": kwargs.get("deep"), "priority": kwargs.get("priority")})
        return [SiteResult("site1", True, deep_offers, elapsed=15.0)]

    monkeypatch.setattr("avia.bot.search", mock_search)

    # Первое нажатие «🔬 Глубокий поиск»
    cq1 = {
        "id": "cq_deep_1",
        "from": {"id": chat_id},
        "data": f"deep:{d_id}",
        "message": {"chat": {"id": chat_id}, "message_id": 100},
    }
    await bot.handle_callback(cq1)

    # 1. Поиск был вызван с deep=True
    assert len(search_calls) == 1
    assert search_calls[0]["deep"] is True
    assert search_calls[0]["priority"] == 0

    # 2. Метрика записана с cls='deep'
    with bot.metrics._connect() as db:
        row = db.execute("SELECT source, sites_total, sites_ok, cls FROM searches ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["source"] == "user"
    assert row["cls"] == "deep"

    # 3. Отчёт содержит первую строку «🔬 Глубокий поиск: полная выдача»
    send_calls = [p for m, p in bot.calls if m == "sendMessage" and "Глубокий поиск" in p.get("text", "")]
    assert len(send_calls) >= 1
    report_text = send_calls[-1]["text"]
    assert report_text.splitlines()[0] == "🔬 Глубокий поиск: полная выдача"

    # 4. Проверяем, что 1 поиск списался из дневного лимита
    user_info = bot.users.get(chat_id)
    assert user_info is not None
    assert user_info.total_searches == 1

    # 5. Повторное нажатие в течение TTL (результат из кеша)
    search_calls.clear()
    bot.calls.clear()

    cq2 = {
        "id": "cq_deep_2",
        "from": {"id": chat_id},
        "data": f"deep:{d_id}",
        "message": {"chat": {"id": chat_id}, "message_id": 101},
    }
    await bot.handle_callback(cq2)

    # Поиск НЕ вызывался повторно
    assert len(search_calls) == 0

    # Дневной лимит НЕ списывался повторно
    user_info_after = bot.users.get(chat_id)
    assert user_info_after.total_searches == 1

    # Отчёт отправлен из кеша с пометкой о возрасте кеша
    cached_sends = [p for m, p in bot.calls if m == "sendMessage" and "Глубокий поиск" in p.get("text", "")]
    assert len(cached_sends) >= 1
    cached_report = cached_sends[-1]["text"]
    assert cached_report.splitlines()[0] == "🔬 Глубокий поиск: полная выдача"
    assert "из кеша, возраст" in cached_report


# (г) Миграция searches добавляет cls в старую базу
def test_searches_migration_adds_cls(tmp_path):
    """Миграция _ensure_schema добавляет колонку cls TEXT в существующую таблицу searches."""
    db_path = tmp_path / "old_metrics.sqlite"
    # Создаём старую схему searches без колонки cls
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE searches (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             REAL NOT NULL,
                source         TEXT NOT NULL,
                total_seconds  REAL NOT NULL,
                first_seconds  REAL,
                sites_total    INTEGER NOT NULL,
                sites_ok       INTEGER NOT NULL,
                queue_seconds  REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO searches (ts, source, total_seconds, first_seconds, sites_total, sites_ok, queue_seconds) "
            "VALUES (1000.0, 'user', 25.0, 10.0, 5, 4, 1.0)"
        )

    # Инициализируем MetricsStore — он должен выполнить миграцию
    store = MetricsStore(db_path)
    store._ensure_schema()

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(searches)").fetchall()}
        assert "cls" in cols

        # Старая строка сохранена
        row_old = conn.execute("SELECT ts, source, cls FROM searches WHERE id = 1").fetchone()
        assert row_old[0] == 1000.0
        assert row_old[1] == "user"
        assert row_old[2] is None

    # Запись нового поиска с явным cls='deep'
    results = [SiteResult(site="s", ok=True, offers=[Offer("s", 1000)], elapsed=5.0)]
    store.record_search("user", 5.0, results, cls="deep")

    with sqlite3.connect(db_path) as conn:
        row_new = conn.execute("SELECT source, cls FROM searches WHERE id = 2").fetchone()
        assert row_new[0] == "user"
        assert row_new[1] == "deep"

    # Запись нового поиска по умолчанию (cls='fast')
    store.record_search("user", 6.0, results)

    with sqlite3.connect(db_path) as conn:
        row_def = conn.execute("SELECT source, cls FROM searches WHERE id = 3").fetchone()
        assert row_def[0] == "user"
        assert row_def[1] == "fast"
