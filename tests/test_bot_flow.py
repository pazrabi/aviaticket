from __future__ import annotations

import asyncio
from datetime import date

import pytest

from avia.bot import Bot
from avia.config import Settings
from avia.models import Offer, Query, SiteResult
from avia.store import ResultStore


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    """Изолируем MetricsStore в тестах бота, чтобы они не писали в реальный state."""
    import avia.bot
    from avia.metrics import MetricsStore

    monkeypatch.setattr(avia.bot, "MetricsStore", lambda _path: MetricsStore(tmp_path / "metrics.sqlite"))


class RecordingBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.next_msg_id = 200
        self.delete_error = False

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        if method == "answerCallbackQuery":
            return True
        if method == "deleteMessage":
            if self.delete_error:
                raise RuntimeError("Telegram API error: cannot delete message")
            return True
        if method == "editMessageText":
            return True
        if method == "getUpdates":
            return []
        return {"ok": True}


def make_test_bot(allowed_ids: set[int] | None = None) -> RecordingBot:
    settings = Settings(
        sites=[],
        tg_token="secret_test_token_12345",
        tg_allowed_ids=allowed_ids or {123},
    )
    return RecordingBot(settings)


@pytest.mark.asyncio
async def test_bot_access_control():
    """Доступ только для разрешённых пользователей (message и callback)."""
    bot = make_test_bot(allowed_ids={123})

    # 1. Сообщение от неразрешённого пользователя (id=999) - молча игнорируется
    unauth_msg = {
        "chat": {"id": 10},
        "from": {"id": 999},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(unauth_msg)
    assert len(bot.calls) == 0

    # 2. Callback от неразрешённого пользователя (id=999) - молча игнорируется
    unauth_cq = {
        "id": "cq_unauth",
        "chat_instance": "ci1",
        "message": {"chat": {"id": 10}, "message_id": 100},
        "from": {"id": 999},
        "data": "menu:go",
    }
    await bot.handle_callback(unauth_cq)
    assert len(bot.calls) == 0

    # 3. Сообщение от разрешённого пользователя (id=123) - обрабатывается
    auth_msg = {
        "chat": {"id": 10},
        "from": {"id": 123},
        "text": "/start",
    }
    await bot.handle(auth_msg)
    assert len(bot.calls) > 0
    assert any(m == "sendMessage" for m, _ in bot.calls)


@pytest.mark.asyncio
async def test_bot_answer_callback_query_always_called():
    """answerCallbackQuery ВСЕГДА вызывается для разрешённого пользователя."""
    bot = make_test_bot(allowed_ids={123})

    # Сценарий 1: Кнопка главного меню
    bot.calls.clear()
    cq_menu = {
        "id": "cq_menu_1",
        "message": {"chat": {"id": 10}, "message_id": 101},
        "from": {"id": 123},
        "data": "menu:go",
    }
    await bot.handle_callback(cq_menu)
    ans_calls = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans_calls) == 1
    assert ans_calls[0]["callback_query_id"] == "cq_menu_1"

    # Сценарий 2: Устаревший callback (чужой dialog_id)
    bot.calls.clear()
    cq_expired = {
        "id": "cq_exp_2",
        "message": {"chat": {"id": 10}, "message_id": 102},
        "from": {"id": 123},
        "data": "bag:wrong_dialog_id:with",
    }
    await bot.handle_callback(cq_expired)
    ans_calls = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans_calls) == 1
    assert ans_calls[0]["callback_query_id"] == "cq_exp_2"
    assert ans_calls[0]["text"] == "Диалог устарел, начните заново"

    # Сценарий 3: Раздел отслеживания (menu:track)
    bot.calls.clear()
    cq_track = {
        "id": "cq_track_3",
        "message": {"chat": {"id": 10}, "message_id": 103},
        "from": {"id": 123},
        "data": "menu:track",
    }
    await bot.handle_callback(cq_track)
    ans_calls = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(ans_calls) == 1
    assert ans_calls[0]["callback_query_id"] == "cq_track_3"


@pytest.mark.asyncio
async def test_bot_busy_protection(monkeypatch):
    """Один активный поиск на чат (busy-защита)."""
    bot = make_test_bot(allowed_ids={123})
    started = asyncio.Event()

    async def slow_search(q, s):
        started.set()
        await asyncio.sleep(0.05)
        return [SiteResult("site1", True, [Offer("site1", 5000)])]

    monkeypatch.setattr("avia.bot.search", slow_search)

    msg = {
        "chat": {"id": 10},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }

    task1 = asyncio.create_task(bot.handle(msg))
    await started.wait()

    # Второй параллельный запрос в тот же чат
    await bot.handle(msg)

    # Проверяем, что отправлено сообщение о занятости
    busy_messages = [
        p["text"]
        for m, p in bot.calls
        if m == "sendMessage" and "Предыдущий поиск ещё идёт" in p["text"]
    ]
    assert len(busy_messages) == 1

    await task1
    assert 10 not in bot.busy


@pytest.mark.asyncio
async def test_bot_wait_msg_deleted_or_edited(monkeypatch):
    """Сообщение «Ищу…» удаляется (deleteMessage), а если не вышло — editMessageText на «Готово»."""
    bot = make_test_bot(allowed_ids={123})

    async def mock_search(q, s):
        return [SiteResult("site1", True, [Offer("site1", 5000)])]

    monkeypatch.setattr("avia.bot.search", mock_search)

    # 1. Успешное удаление deleteMessage
    msg1 = {
        "chat": {"id": 20},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg1)

    # Находим id сообщения «Ищу...»
    wait_calls = [
        p for m, p in bot.calls if m == "sendMessage" and "Ищу билеты" in p["text"]
    ]
    assert len(wait_calls) == 1
    # Проверяем, что был вызов deleteMessage
    del_calls = [m for m, _ in bot.calls if m == "deleteMessage"]
    assert len(del_calls) == 1

    # 2. Ошибка при deleteMessage -> editMessageText на «Готово»
    bot.calls.clear()
    bot.delete_error = True
    msg2 = {
        "chat": {"id": 21},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg2)

    edit_calls = [p for m, p in bot.calls if m == "editMessageText" and p.get("text") == "Готово"]
    assert len(edit_calls) == 1


@pytest.mark.asyncio
async def test_bot_long_report_multiple_messages(monkeypatch):
    """Длинный отчёт уходит несколькими sendMessage, send() режет по строкам при >4096."""
    bot = make_test_bot(allowed_ids={123})

    # Сценарий 1: build_report возвращает несколько частей (частей отчёта)
    async def mock_search(q, s):
        return [SiteResult("site1", True, [Offer("site1", 5000)])]

    monkeypatch.setattr("avia.bot.search", mock_search)
    monkeypatch.setattr(
        "avia.bot.build_compact_report",
        lambda q, res, criteria, **kw: (["Часть 1 отчёта", "Часть 2 отчёта", "Часть 3 отчёта"], [], []),
    )

    msg = {
        "chat": {"id": 30},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    part_messages = [
        p["text"]
        for m, p in bot.calls
        if m == "sendMessage" and "отчёта" in p["text"]
    ]
    assert part_messages == ["Часть 1 отчёта", "Часть 2 отчёта", "Часть 3 отчёта"]

    # Сценарий 2: send() получает текст больше 4096 символов
    bot.calls.clear()
    long_text = "\n".join(f"Строка {i:04d} с текстом отчёта билетов" for i in range(150))
    assert len(long_text) > 4096

    await bot.send(30, long_text)
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) > 1
    for call in send_calls:
        assert len(call["text"]) <= 4096


@pytest.mark.asyncio
async def test_bot_price_command_works(monkeypatch):
    """Команда /price работает корректно."""
    bot = make_test_bot(allowed_ids={123})

    async def mock_search(q, s):
        offer = Offer("site_test", 12345, airline="TestAir")
        return [SiteResult("site_test", True, [offer])]

    monkeypatch.setattr("avia.bot.search", mock_search)

    msg = {
        "chat": {"id": 40},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12 2",
    }
    await bot.handle(msg)

    # Проверяем отправленные сообщения
    send_texts = [p["text"] for m, p in bot.calls if m == "sendMessage"]
    assert any("Ищу билеты" in t for t in send_texts)
    assert any("TestAir" in t and "12 345" in t for t in send_texts)

    # Проверяем, что снимок сохранился в store
    snap = bot.store.get(40)
    assert snap is not None
    assert snap.query.origin == "MOW"
    assert snap.query.destination == "ALA"
    assert snap.query.adults == 2
    assert len(snap.flat) == 1


@pytest.mark.asyncio
async def test_bot_run_allowed_updates():
    """run() передаёт allowed_updates=['message', 'callback_query'] и обрабатывает оба типа."""
    bot = make_test_bot(allowed_ids={123})
    handled_messages = []
    handled_callbacks = []

    async def fake_handle(msg):
        handled_messages.append(msg)

    async def fake_handle_callback(cq):
        handled_callbacks.append(cq)

    bot.handle = fake_handle
    bot.handle_callback = fake_handle_callback

    updates_sample = [
        {"update_id": 1, "message": {"chat": {"id": 10}, "text": "hi"}},
        {"update_id": 2, "callback_query": {"id": "cq1", "data": "menu:go"}},
    ]
    iterations = 0

    async def mock_call(method: str, **params):
        nonlocal iterations
        bot.calls.append((method, params))
        if method == "getUpdates":
            iterations += 1
            if iterations == 1:
                return updates_sample
            raise asyncio.CancelledError()
        return {}

    bot.call = mock_call

    with pytest.raises(asyncio.CancelledError):
        await bot.run()

    await asyncio.sleep(0.01)

    get_updates_calls = [p for m, p in bot.calls if m == "getUpdates"]
    assert len(get_updates_calls) >= 1
    assert get_updates_calls[0].get("allowed_updates") == ["message", "callback_query"]

    assert len(handled_messages) == 1
    assert handled_messages[0]["text"] == "hi"
    assert len(handled_callbacks) == 1
    assert handled_callbacks[0]["data"] == "menu:go"

