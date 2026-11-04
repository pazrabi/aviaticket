"""Тесты фонового сбора цен (BackgroundCollector, настройки и интеграция с Bot)."""
from __future__ import annotations

import asyncio
from datetime import date
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from avia.bg_collector import BackgroundCollector
from avia.bot import Bot
from avia.config import Settings, load_settings
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult


def _make_query() -> Query:
    return Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))


def _make_settings(**kwargs) -> Settings:
    defaults = {
        "sites": [],
        "tg_token": "secret",
        "tg_allowed_ids": {1},
        "bg_collect": True,
        "bg_collect_interval_minutes": 20,
        "bg_collect_max_per_day": 48,
        "bg_collect_recheck_hours": 6,
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def test_settings_bg_collect_defaults_and_env(monkeypatch):
    """Настройки фонового сбора считываются из env или используют значения по умолчанию."""
    # 1. По умолчанию
    s_def = Settings(sites=[])
    assert s_def.bg_collect is False
    assert s_def.bg_collect_interval_minutes == 20
    assert s_def.bg_collect_max_per_day == 48
    assert s_def.bg_collect_recheck_hours == 6

    # 2. Через load_settings с заданными переменными окружения
    monkeypatch.setenv("BG_COLLECT", "1")
    monkeypatch.setenv("BG_COLLECT_INTERVAL_MIN", "15")
    monkeypatch.setenv("BG_COLLECT_MAX_PER_DAY", "100")
    monkeypatch.setenv("BG_COLLECT_RECHECK_HOURS", "8")
    monkeypatch.setenv("TG_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("TG_ALLOWED_IDS", "123")

    s = load_settings()
    assert s.bg_collect is True
    assert s.bg_collect_interval_minutes == 15
    assert s.bg_collect_max_per_day == 100
    assert s.bg_collect_recheck_hours == 8


@pytest.mark.asyncio
async def test_run_once_respects_max_per_day():
    """run_once не запускает поиск, если исчерпан лимит на текущие сутки."""
    history = MagicMock()
    search_fn = AsyncMock()
    is_idle = MagicMock(return_value=True)
    settings = _make_settings(bg_collect_max_per_day=2)

    collector = BackgroundCollector(history, search_fn, is_idle, settings)
    collector._done_today = 2

    res = await collector.run_once()
    assert res is False
    is_idle.assert_not_called()
    history.collect_candidates.assert_not_called()
    search_fn.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_checks_is_idle():
    """run_once возвращает False и не ищет, если телефоны или бот заняты (is_idle = False)."""
    history = MagicMock()
    search_fn = AsyncMock()
    is_idle = MagicMock(return_value=False)
    settings = _make_settings()

    collector = BackgroundCollector(history, search_fn, is_idle, settings)

    res = await collector.run_once()
    assert res is False
    is_idle.assert_called_once()
    history.collect_candidates.assert_not_called()
    search_fn.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_no_candidates():
    """run_once возвращает False, если нет кандидатов для обновления."""
    history = MagicMock()
    history.collect_candidates.return_value = []
    search_fn = AsyncMock()
    is_idle = MagicMock(return_value=True)
    settings = _make_settings()

    collector = BackgroundCollector(history, search_fn, is_idle, settings)

    res = await collector.run_once()
    assert res is False
    search_fn.assert_not_called()
    assert collector.done_today == 0


@pytest.mark.asyncio
async def test_run_once_search_fn_error_resilience():
    """Ошибка search_fn (капча, сбой сервиса) ловится, не роняет выполнение и не записывает annotate."""
    q = _make_query()
    history = MagicMock()
    history.collect_candidates.return_value = [q]
    search_fn = AsyncMock(side_effect=RuntimeError("502 Bad Gateway / Captcha"))
    is_idle = MagicMock(return_value=True)
    settings = _make_settings()

    collector = BackgroundCollector(history, search_fn, is_idle, settings)

    res = await collector.run_once()
    assert res is False
    search_fn.assert_called_once_with(q)
    history.annotate.assert_not_called()
    assert collector.done_today == 0


@pytest.mark.asyncio
async def test_run_once_success_annotates_and_increments():
    """При успешном поиске вызывается annotate (с assign_ids) и увеличивается счётчик суток."""
    q = _make_query()
    history = MagicMock()
    history.collect_candidates.return_value = [q]
    offer = Offer("aviasales", 5000)
    assert offer.offer_id == ""
    assert offer.fetched_at is None
    site_res = [SiteResult("aviasales", True, [offer])]
    search_fn = AsyncMock(return_value=site_res)
    is_idle = MagicMock(return_value=True)
    fixed_now = 1700000000.0
    fixed_today = date(2026, 11, 4)
    settings = _make_settings(bg_collect_recheck_hours=5)

    collector = BackgroundCollector(
        history,
        search_fn,
        is_idle,
        settings,
        clock=lambda: fixed_now,
        today=lambda: fixed_today,
    )

    res = await collector.run_once()
    assert res is True
    assert offer.offer_id != ""
    assert offer.fetched_at == fixed_now
    history.collect_candidates.assert_called_once_with(
        limit=1,
        today=fixed_today,
        now=fixed_now,
        recheck_seconds=5 * 3600.0,
    )
    search_fn.assert_called_once_with(q)
    history.annotate.assert_called_once_with(q, site_res, fixed_now)
    assert collector.done_today == 1


@pytest.mark.asyncio
async def test_run_once_with_real_price_history_saves_offers_without_initial_id(tmp_path):
    """Фоновый сбор сохраняет офферы без исходного offer_id в реальную БД через assign_ids + annotate."""
    db_path = tmp_path / "prices.sqlite"
    history = PriceHistory(db_path)
    q = _make_query()

    fixed_now = 1700000000.0
    history.annotate(
        q,
        [SiteResult("aviasales", True, [Offer("aviasales", 6000, offer_id="seed1")])],
        now=fixed_now - 10 * 3600,
    )

    candidates = history.collect_candidates(limit=1, today=q.depart, now=fixed_now)
    assert len(candidates) == 1
    assert candidates[0].destination == q.destination

    raw_offer = Offer("aviasales", 4500)
    assert raw_offer.offer_id == ""
    assert raw_offer.fetched_at is None
    site_res = [SiteResult("aviasales", True, [raw_offer])]
    search_fn = AsyncMock(return_value=site_res)
    is_idle = MagicMock(return_value=True)
    settings = _make_settings()

    collector = BackgroundCollector(
        history,
        search_fn,
        is_idle,
        settings,
        clock=lambda: fixed_now,
        today=lambda: q.depart,
    )

    res = await collector.run_once()
    assert res is True
    assert raw_offer.offer_id != ""
    assert raw_offer.fetched_at == fixed_now
    assert collector.done_today == 1

    with history._connect() as db:
        rows = db.execute(
            "SELECT offer_id, price, observed_at FROM price_history WHERE price = 4500"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == raw_offer.offer_id
        assert rows[0][1] == 4500
        assert rows[0][2] == fixed_now


@pytest.mark.asyncio
async def test_run_once_date_change_resets_counter():
    """Смена суток сбрасывает счётчик done_today."""
    q = _make_query()
    history = MagicMock()
    history.collect_candidates.return_value = [q]
    search_fn = AsyncMock(return_value=[])
    is_idle = MagicMock(return_value=True)
    settings = _make_settings(bg_collect_max_per_day=5)

    cur_d = date(2026, 11, 4)

    collector = BackgroundCollector(
        history,
        search_fn,
        is_idle,
        settings,
        today=lambda: cur_d,
    )

    # 1-й день: делаем поиск
    await collector.run_once()
    assert collector.done_today == 1

    # Наступил следующий день
    cur_d = date(2026, 11, 5)
    assert collector.done_today == 0

    # Новый поиск увеличивает счётчик уже нового дня
    await collector.run_once()
    assert collector.done_today == 1


@pytest.mark.asyncio
async def test_run_forever_sleeps_interval_before_first_run(monkeypatch):
    """run_forever делает паузу interval перед первым запуском, а не запускается мгновенно."""
    settings = _make_settings(bg_collect_interval_minutes=10)
    history = MagicMock()
    search_fn = AsyncMock()
    is_idle = MagicMock(return_value=True)

    collector = BackgroundCollector(history, search_fn, is_idle, settings)

    events: list[str] = []

    async def fake_sleep(seconds: float):
        events.append(f"sleep:{seconds}")
        if len(events) >= 2:
            raise asyncio.CancelledError()

    async def fake_run_once():
        events.append("run_once")
        return True

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(collector, "run_once", fake_run_once)

    with pytest.raises(asyncio.CancelledError):
        await collector.run_forever()

    # Проверяем, что первый вызов — sleep на 600 секунд (10 мин * 60)
    assert events == ["sleep:600.0", "run_once", "sleep:600.0"]


@pytest.mark.asyncio
async def test_bot_active_searches_and_idle_state(tmp_path):
    """Bot корректно отслеживает _active_searches и состояние _is_idle."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = _make_settings(storage_dir=storage, bg_collect=True)

    bot = Bot(settings)
    assert bot._is_idle() is True

    # 1. Занят чат человека
    bot.busy.add(12345)
    assert bot._is_idle() is False
    bot.busy.clear()
    assert bot._is_idle() is True

    # 2. Идёт активный поиск
    bot._active_searches = 1
    assert bot._is_idle() is False
    bot._active_searches = 0
    assert bot._is_idle() is True

    # 3. Collector создан в Bot
    assert bot.bg_collector is not None
    assert bot.bg_collector.done_today == 0
