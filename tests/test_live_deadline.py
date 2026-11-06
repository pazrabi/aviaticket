"""Тесты для шага 5б: дедлайн живого поиска, выдача по мере готовности и досылка опоздавших."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import time
from typing import Any

import pytest

from avia.bot import Bot, WaitMessageUpdater
from avia.config import Settings, SiteConfig
from avia.models import Offer, Query, SiteResult
from avia.pipeline import search


class RecordingBot(Bot):
    """Тестовый бот, протоколирующий все API-вызовы Telegram."""

    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_msg_id = 100

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
        if method == "getUpdates":
            return []
        return {"ok": True}


@pytest.fixture(autouse=True)
def _isolate_bot_state(tmp_path, monkeypatch):
    """Изолируем MetricsStore в тестах, чтобы не писать в реальные базы."""
    import avia.bot
    from avia.metrics import MetricsStore

    monkeypatch.setattr(avia.bot, "MetricsStore", lambda _path: MetricsStore(tmp_path / "metrics.sqlite"))


def make_bot(tmp_path: Path, sites: list[SiteConfig] | None = None, deadline: float = 30.0) -> RecordingBot:
    settings = Settings(
        sites=sites or [],
        tg_token="dummy_token_live_deadline",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
        live_deadline_seconds=deadline,
    )
    return RecordingBot(settings)


@pytest.mark.asyncio
async def test_live_deadline_all_sites_fast(tmp_path, monkeypatch):
    """(а) Все сайты быстрее дедлайна — карточка формируется как раньше, без досылок."""
    sites = [
        SiteConfig(name="fast1", url_template="http://fast1"),
        SiteConfig(name="fast2", url_template="http://fast2"),
    ]
    bot = make_bot(tmp_path, sites=sites, deadline=2.0)

    async def fast_search(q, s, priority=0, on_site_done=None, **kwargs):
        res1 = SiteResult("fast1", True, [Offer("fast1", 5000, airline="Air1")])
        if on_site_done:
            await on_site_done(res1)
        res2 = SiteResult("fast2", True, [Offer("fast2", 6000, airline="Air2")])
        if on_site_done:
            await on_site_done(res2)
        return [res1, res2]

    monkeypatch.setattr("avia.bot.search", fast_search)

    msg = {
        "chat": {"id": 123},
        "from": {"id": 123},
        "text": "/price MOW LED 10.12",
    }
    await bot.handle(msg)

    # Сообщение ожидания содержит имена сайтов и новый текст
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert any("обычно до 30 секунд" in p["text"] for p in send_calls)
    assert not any("это займёт несколько минут" in p["text"] for p in send_calls)

    # Карточка отправлена пользователю
    assert any("5 000 ₽" in p["text"] for p in send_calls)

    # Сообщение ожидания удалено
    del_calls = [m for m, _ in bot.calls if m == "deleteMessage"]
    assert len(del_calls) >= 1

    # Никаких оповещений о досылке не отправлялось
    assert not any("➕ Опоздал сайт" in p["text"] for p in send_calls)


@pytest.mark.asyncio
async def test_live_deadline_slow_site_cheaper_sends_notification(tmp_path, monkeypatch):
    """(б) Один сайт медленнее дедлайна — карточка уходит без него, затем приходит досылка (если дешевле)."""
    sites = [
        SiteConfig(name="fast_site", url_template="http://fast"),
        SiteConfig(name="slow_site", url_template="http://slow"),
    ]
    bot = make_bot(tmp_path, sites=sites, deadline=0.2)

    async def slow_search(q, s, priority=0, on_site_done=None, **kwargs):
        res_fast = SiteResult("fast_site", True, [Offer("fast_site", 8000, airline="FastAir")])
        if on_site_done:
            await on_site_done(res_fast)
        # Сайт slow_site отвечает заметно дольше дедлайна (0.2 с)
        await asyncio.sleep(0.35)
        res_slow = SiteResult(
            "slow_site",
            True,
            [Offer("slow_site", 6500, airline="SlowAir")],
            page_url="https://slow.test/search",
        )
        if on_site_done:
            await on_site_done(res_slow)
        return [res_fast, res_slow]

    monkeypatch.setattr("avia.bot.search", slow_search)

    msg = {
        "chat": {"id": 123},
        "from": {"id": 123},
        "text": "/price MOW LED 10.12",
    }
    t0 = time.monotonic()
    await bot.handle(msg)
    elapsed = time.monotonic() - t0

    # Карточка ушла человеку в районе дедлайна (~0.2 с), не дожидаясь slow_site (0.35 с)
    assert elapsed < 0.32

    # Проверяем, что в первой карточке указан статус slow_site с ошибкой о дедлайне
    send_texts = [p["text"] for m, p in bot.calls if m == "sendMessage"]
    assert any("не успел к дедлайну, результат придёт отдельным сообщением" in t for t in send_texts)
    assert any("8 000 ₽" in t for t in send_texts)

    # Ждём завершения фоновой догоняющей задачи
    await asyncio.sleep(0.25)

    # Проверяем, что после завершения пришла досылка с более дешёвой ценой и ссылкой
    send_texts_after = [p["text"] for m, p in bot.calls if m == "sendMessage"]
    late_msgs = [t for t in send_texts_after if "➕ Опоздал сайт slow_site" in t]
    assert len(late_msgs) == 1
    late_text = late_msgs[0]
    assert "нашёл дешевле — 6 500 ₽ (в карточке было 8 000 ₽)" in late_text
    assert "https://slow.test/search" in late_text


@pytest.mark.asyncio
async def test_live_deadline_slow_site_expensive_no_notification(tmp_path, monkeypatch):
    """(б) Один сайт медленнее дедлайна — если билет дороже, досылка НЕ отправляется."""
    sites = [
        SiteConfig(name="fast_site", url_template="http://fast"),
        SiteConfig(name="slow_site", url_template="http://slow"),
    ]
    bot = make_bot(tmp_path, sites=sites, deadline=0.2)

    async def slow_search(q, s, priority=0, on_site_done=None, **kwargs):
        res_fast = SiteResult("fast_site", True, [Offer("fast_site", 5000, airline="FastAir")])
        if on_site_done:
            await on_site_done(res_fast)
        await asyncio.sleep(0.35)
        # slow_site нашёл дороже (9000 > 5000)
        res_slow = SiteResult(
            "slow_site",
            True,
            [Offer("slow_site", 9000, airline="SlowAir")],
            page_url="https://slow.test/search",
        )
        if on_site_done:
            await on_site_done(res_slow)
        return [res_fast, res_slow]

    monkeypatch.setattr("avia.bot.search", slow_search)

    msg = {
        "chat": {"id": 123},
        "from": {"id": 123},
        "text": "/price MOW LED 10.12",
    }
    await bot.handle(msg)

    # Ждём фоновую задачу
    await asyncio.sleep(0.25)

    send_texts = [p["text"] for m, p in bot.calls if m == "sendMessage"]
    # Никакой досылки не отправлялось
    assert not any("➕ Опоздал сайт" in t for t in send_texts)


@pytest.mark.asyncio
async def test_live_deadline_zero_disables_deadline(tmp_path, monkeypatch):
    """(в) live_deadline_seconds=0 — прежнее поведение (ждёт все сайты)."""
    sites = [
        SiteConfig(name="site1", url_template="http://site1"),
        SiteConfig(name="site2", url_template="http://site2"),
    ]
    bot = make_bot(tmp_path, sites=sites, deadline=0)

    async def slow_search(q, s, priority=0, on_site_done=None, **kwargs):
        res1 = SiteResult("site1", True, [Offer("site1", 4000)])
        if on_site_done:
            await on_site_done(res1)
        await asyncio.sleep(0.15)
        res2 = SiteResult("site2", True, [Offer("site2", 3500)])
        if on_site_done:
            await on_site_done(res2)
        return [res1, res2]

    monkeypatch.setattr("avia.bot.search", slow_search)

    msg = {
        "chat": {"id": 123},
        "from": {"id": 123},
        "text": "/price MOW LED 10.12",
    }
    t0 = time.monotonic()
    await bot.handle(msg)
    elapsed = time.monotonic() - t0

    # Дождался оба сайта
    assert elapsed >= 0.14
    send_texts = [p["text"] for m, p in bot.calls if m == "sendMessage"]
    # Оба сайта попали в первоначальную карточку
    assert not any("не успел к дедлайну" in t for t in send_texts)
    assert not any("➕ Опоздал сайт" in t for t in send_texts)


@pytest.mark.asyncio
async def test_pipeline_search_exception_in_on_site_done_ignored(tmp_path, monkeypatch):
    """(г) Исключение в on_site_done не ломает pipeline.search."""
    settings = Settings(
        sites=[
            SiteConfig(name="site1", url_template="http://s1", source="api", parser="travelpayouts"),
            SiteConfig(name="site2", url_template="http://s2", source="api", parser="travelpayouts"),
        ],
        tg_token="dummy",
        storage_dir=tmp_path,
    )
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))

    async def fake_from_api(site, q, settings):
        return [Offer(site.name, 5000)], f"http://{site.name}"

    monkeypatch.setattr("avia.pipeline._from_api", fake_from_api)

    # 1. Синхронный колбэк с исключением
    def bad_sync_callback(res):
        raise ValueError("Синхронный сбой в on_site_done")

    res = await search(q, settings, on_site_done=bad_sync_callback)
    assert len(res) == 2
    assert res[0].site == "site1" and res[0].ok
    assert res[1].site == "site2" and res[1].ok

    # 2. Асинхронный колбэк с исключением
    async def bad_async_callback(res):
        raise RuntimeError("Асинхронный сбой в on_site_done")

    res = await search(q, settings, on_site_done=bad_async_callback)
    assert len(res) == 2
    assert res[0].site == "site1" and res[0].ok
    assert res[1].site == "site2" and res[1].ok


@pytest.mark.asyncio
async def test_wait_message_throttle_interval_with_fake_clock():
    """(д) Редактирование сообщения ожидания не чаще интервала 1.5 с (поддельные часы)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class DummyBot:
        async def call(self, method: str, **params):
            calls.append((method, params))
            return True

    bot = DummyBot()
    sites = [SiteConfig(name=f"site_{i}", url_template="") for i in range(1, 6)]

    current_time = 100.0

    updater = WaitMessageUpdater(
        bot=bot,  # type: ignore[arg-type]
        chat_id=123,
        message_id=456,
        sites=sites,
        header="Header",
        footer="Footer",
        min_interval=1.5,
    )

    import time
    original_monotonic = time.monotonic

    def fake_monotonic():
        return current_time

    time.monotonic = fake_monotonic
    try:
        # t = 100.0: сайт 1 завершился -> первый вызов editMessageText
        await updater.on_site_done(SiteResult("site_1", True, [Offer("site_1", 5000)]))
        assert len(calls) == 1
        assert "site_1" in calls[-1][1]["text"]

        # t = 100.5: сайт 2 завершился -> прошло 0.5 с < 1.5 с, вызов НЕ должен произойти
        current_time = 100.5
        await updater.on_site_done(SiteResult("site_2", True, [Offer("site_2", 4000)]))
        assert len(calls) == 1

        # t = 101.0: сайт 3 завершился -> прошло 1.0 с < 1.5 с, вызов НЕ должен произойти
        current_time = 101.0
        await updater.on_site_done(SiteResult("site_3", True, [Offer("site_3", 3000)]))
        assert len(calls) == 1

        # t = 101.6: сайт 4 завершился -> прошло 1.6 с >= 1.5 с, вызов ДОЛЖЕН произойти
        current_time = 101.6
        await updater.on_site_done(SiteResult("site_4", True, [Offer("site_4", 2000)]))
        assert len(calls) == 2
        assert "site_4" in calls[-1][1]["text"]

        # t = 102.0: сайт 5 завершился -> прошло 0.4 с < 1.5 с, троттлится
        current_time = 102.0
        await updater.on_site_done(SiteResult("site_5", True, [Offer("site_5", 1000)]))
        assert len(calls) == 2

        # В конце вызывается flush() -> обновляет текст независимо от интервала
        current_time = 102.1
        await updater.flush()
        assert len(calls) == 3
        assert "site_5" in calls[-1][1]["text"]
    finally:
        time.monotonic = original_monotonic


@pytest.mark.asyncio
async def test_user_can_start_new_search_while_late_search_running(tmp_path, monkeypatch):
    """Пользователь может запустить новый поиск, пока старый догоняется в фоне (chat_id не в busy)."""
    sites = [
        SiteConfig(name="fast", url_template="http://fast"),
        SiteConfig(name="slow", url_template="http://slow"),
    ]
    bot = make_bot(tmp_path, sites=sites, deadline=0.1)

    async def slow_search(q, s, priority=0, on_site_done=None, **kwargs):
        res_fast = SiteResult("fast", True, [Offer("fast", 8000)])
        if on_site_done:
            await on_site_done(res_fast)
        await asyncio.sleep(0.3)
        res_slow = SiteResult("slow", True, [Offer("slow", 5000)])
        if on_site_done:
            await on_site_done(res_slow)
        return [res_fast, res_slow]

    monkeypatch.setattr("avia.bot.search", slow_search)

    msg1 = {
        "chat": {"id": 123},
        "from": {"id": 123},
        "text": "/price MOW LED 10.12",
    }
    # Первый поиск отдаёт карточку по дедлайну
    await bot.handle(msg1)
    assert 123 not in bot.busy

    # Второй поиск запускается сразу же, пока первый догоняется
    msg2 = {
        "chat": {"id": 123},
        "from": {"id": 123},
        "text": "/price MOW AER 15.12",
    }
    await bot.handle(msg2)

    # Проверяем, что не было сообщения о занятости
    busy_msgs = [p["text"] for m, p in bot.calls if m == "sendMessage" and "Предыдущий поиск ещё идёт" in p["text"]]
    assert len(busy_msgs) == 0

    # Дожидаемся завершения фоновых задач
    await asyncio.sleep(0.35)
