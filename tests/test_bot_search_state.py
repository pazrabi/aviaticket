from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from avia.bot import Bot
from avia.config import Settings
from avia.dialog import Step
from avia.models import Offer, Query, SiteResult


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
        self.answer_cb_error = False
        self.send_message_error_text: str | None = None

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            if self.send_message_error_text and self.send_message_error_text in params.get("text", ""):
                raise RuntimeError(f"Telegram API error: cannot send {self.send_message_error_text}")
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        if method == "answerCallbackQuery":
            if self.answer_cb_error:
                raise RuntimeError("query is too old")
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


def make_test_bot(
    tmp_path,
    allowed_ids: set[int] | None = None,
    tg_open: bool = True,
    daily_limit: int = 5,
) -> RecordingBot:
    settings = Settings(
        sites=[],
        tg_token="secret_test_token_12345",
        tg_allowed_ids=allowed_ids or {123},
        tg_open=tg_open,
        daily_search_limit=daily_limit,
        storage_dir=tmp_path,
    )
    return RecordingBot(settings)


@pytest.mark.asyncio
async def test_search_exception_resets_searching_step(tmp_path, monkeypatch):
    """После исключения поиска шаг сессии не SEARCHING, ошибка с кнопкой «Найти билет», и бот отвечает на новый текст нормально."""
    bot = make_test_bot(tmp_path, allowed_ids={123})

    async def failing_search(q, s, **kwargs):
        raise RuntimeError("Site crashed")

    monkeypatch.setattr("avia.bot.search", failing_search)

    chat_id = 10
    msg = {
        "chat": {"id": chat_id},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    # Шаг сессии сброшен в DONE
    session = bot.dialog.sessions.get(chat_id)
    assert session is not None
    assert session.step == Step.DONE

    # Сообщение об ошибке отправлено с кнопкой «🔎 Найти билет» (callback menu:go)
    err_calls = [
        p for m, p in bot.calls
        if m == "sendMessage" and "⚠️" in p.get("text", "")
    ]
    assert len(err_calls) == 1
    kb = err_calls[0].get("reply_markup", {}).get("inline_keyboard", [])
    assert any(btn.get("callback_data") == "menu:go" for row in kb for btn in row)

    # Следующее сообщение от пользователя не получает «Поиск ещё идёт»
    bot.calls.clear()
    next_msg = {
        "chat": {"id": chat_id},
        "from": {"id": 123},
        "text": "Привет",
    }
    await bot.handle(next_msg)
    sent_texts = [p.get("text", "") for m, p in bot.calls if m == "sendMessage"]
    assert not any("Поиск ещё идёт" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_busy_branch_resets_step(tmp_path):
    """Ветка busy сбрасывает шаг сессии из SEARCHING."""
    bot = make_test_bot(tmp_path, allowed_ids={123})
    chat_id = 10

    # Задаём сессию бота в состоянии SEARCHING и помечаем chat_id как занятый
    bot.busy.add(chat_id)

    msg = {
        "chat": {"id": chat_id},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    # Отправлено сообщение о занятости
    busy_calls = [
        p for m, p in bot.calls
        if m == "sendMessage" and "Предыдущий поиск ещё идёт" in p.get("text", "")
    ]
    assert len(busy_calls) == 1

    # Шаг сессии сброшен в DONE
    session = bot.dialog.sessions.get(chat_id)
    assert session is not None
    assert session.step == Step.DONE


@pytest.mark.asyncio
async def test_limit_exhausted_branch_resets_step_and_shows_keyboard(tmp_path):
    """Ветка лимита сбрасывает шаг и показывает клавиатуру меню/«Мои отслеживания»."""
    bot = make_test_bot(tmp_path, allowed_ids={123}, tg_open=True, daily_limit=1)
    chat_id = 456
    user_id = 456

    # Регистрируем и исчерпываем суточный лимит
    bot.users.register(user_id)
    assert bot.users.try_use_search(user_id, daily_limit=1) is True
    assert bot.users.try_use_search(user_id, daily_limit=1) is False

    msg = {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    limit_calls = [
        p for m, p in bot.calls
        if m == "sendMessage" and "лимит поисков исчерпан" in p.get("text", "")
    ]
    assert len(limit_calls) == 1
    # Клавиатура «Мои отслеживания» приложена
    kb = limit_calls[0].get("reply_markup", {}).get("inline_keyboard", [])
    assert any(btn.get("callback_data") == "menu:track" for row in kb for btn in row)

    # Шаг сессии сброшен в DONE
    session = bot.dialog.sessions.get(chat_id)
    assert session is not None
    assert session.step == Step.DONE


@pytest.mark.asyncio
async def test_session_not_overwritten_if_user_started_new_dialog(tmp_path, monkeypatch):
    """Если человек за время поиска начал новый диалог — его сессия не затирается."""
    bot = make_test_bot(tmp_path, allowed_ids={123})
    chat_id = 10

    search_started = asyncio.Event()

    async def slow_search(q, s, **kwargs):
        search_started.set()
        await asyncio.sleep(0.05)
        return [SiteResult("s1", True, [Offer("s1", 5000, offer_id="off1")])]

    monkeypatch.setattr("avia.bot.search", slow_search)

    msg = {
        "chat": {"id": chat_id},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    task = asyncio.create_task(bot.handle(msg))
    await search_started.wait()

    # Пока поиск идёт, пользователь запускает новый диалог через menu:go
    bot.dialog.on_callback(chat_id, "menu:go")
    new_session = bot.dialog.sessions[chat_id]
    assert new_session.step == Step.ORIGIN
    new_dialog_id = new_session.dialog_id

    await task

    # После завершения поиска сессия НЕ перезаписана
    current_session = bot.dialog.sessions[chat_id]
    assert current_session.dialog_id == new_dialog_id
    assert current_session.step == Step.ORIGIN


@pytest.mark.asyncio
async def test_search_refund_on_exception(tmp_path, monkeypatch):
    """Попытка возвращается при исключении поиска."""
    bot = make_test_bot(tmp_path, allowed_ids=set(), tg_open=True, daily_limit=5)
    chat_id = 500
    user_id = 500

    async def failing_search(q, s, **kwargs):
        raise RuntimeError("Search error")

    monkeypatch.setattr("avia.bot.search", failing_search)

    msg = {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    with bot.users._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (user_id,)).fetchone()
        assert row["search_count"] == 0
        assert row["total_searches"] == 0


@pytest.mark.asyncio
async def test_search_refund_on_no_sites_ok(tmp_path, monkeypatch):
    """Попытка возвращается при полном отсутствии результата (нет r.ok ни у одного)."""
    bot = make_test_bot(tmp_path, allowed_ids=set(), tg_open=True, daily_limit=5)
    chat_id = 501
    user_id = 501

    async def all_failed_search(q, s, **kwargs):
        return [
            SiteResult("site1", False, [], error="Timeout"),
            SiteResult("site2", False, [], error="HTTP 500"),
        ]

    monkeypatch.setattr("avia.bot.search", all_failed_search)

    msg = {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    with bot.users._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (user_id,)).fetchone()
        assert row["search_count"] == 0
        assert row["total_searches"] == 0


@pytest.mark.asyncio
async def test_search_not_refunded_on_ok_result(tmp_path, monkeypatch):
    """Попытка НЕ возвращается при честной ok-выдаче (даже если билетов нет)."""
    bot = make_test_bot(tmp_path, allowed_ids=set(), tg_open=True, daily_limit=5)
    chat_id = 502
    user_id = 502

    async def ok_empty_search(q, s, **kwargs):
        return [SiteResult("site1", True, [])]

    monkeypatch.setattr("avia.bot.search", ok_empty_search)

    msg = {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    with bot.users._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (user_id,)).fetchone()
        assert row["search_count"] == 1
        assert row["total_searches"] == 1


@pytest.mark.asyncio
async def test_admin_counter_not_touched(tmp_path, monkeypatch):
    """Админ (в tg_allowed_ids) счётчик не трогает."""
    admin_id = 777
    bot = make_test_bot(tmp_path, allowed_ids={admin_id}, tg_open=True, daily_limit=5)

    async def mock_search(q, s, **kwargs):
        return [SiteResult("site1", True, [Offer("site1", 5000, offer_id="off1")])]

    monkeypatch.setattr("avia.bot.search", mock_search)

    msg = {
        "chat": {"id": admin_id, "type": "private"},
        "from": {"id": admin_id},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    with bot.users._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (admin_id,)).fetchone()
        assert row is None or (row["search_count"] == 0 and row["total_searches"] == 0)


@pytest.mark.asyncio
async def test_chunk_send_failure_preserves_store(tmp_path, monkeypatch):
    """Упавшая отправка второго куска не ломает store (кнопки первого куска работают)."""
    bot = make_test_bot(tmp_path, allowed_ids={123})
    chat_id = 10

    o1 = Offer("site1", 5000, offer_id="off1")
    o2 = Offer("site1", 6000, offer_id="off2")

    async def mock_search(q, s, **kwargs):
        return [SiteResult("site1", True, [o1, o2])]

    monkeypatch.setattr("avia.bot.search", mock_search)
    monkeypatch.setattr(
        "avia.bot.build_compact_report",
        lambda q, res, criteria, **kw: (["Кусок 1", "Кусок 2"], [o1], [o1, o2]),
    )

    bot.send_message_error_text = "Кусок 2"

    msg = {
        "chat": {"id": chat_id},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    # Store сохранён ДО отправки кусков
    snap = bot.store.get(chat_id)
    assert snap is not None
    assert len(snap.flat) == 1
    assert snap.flat[0].offer_id == "off1"

    # Кнопки работают
    session = bot.dialog.sessions.get(chat_id)
    assert session is not None
    res = bot.dialog.on_callback(chat_id, f"more:{session.dialog_id}")
    assert len(res) > 0
    assert not any("устарели" in getattr(r, "text", "") for r in res)


@pytest.mark.asyncio
async def test_answer_callback_query_exception_does_not_block_action(tmp_path):
    """answerCallbackQuery с исключением не блокирует действие."""
    bot = make_test_bot(tmp_path, allowed_ids={123})
    chat_id = 10
    bot.answer_cb_error = True

    cq = {
        "id": "cq_123",
        "chat_instance": "ci1",
        "message": {"chat": {"id": chat_id}, "message_id": 100},
        "from": {"id": 123},
        "data": "menu:go",
    }
    await bot.handle_callback(cq)

    # Действие выполнилось
    session = bot.dialog.sessions.get(chat_id)
    assert session is not None
    assert session.step == Step.ORIGIN
    sent_prompts = [p.get("text", "") for m, p in bot.calls if m == "sendMessage"]
    assert any("Откуда летим" in t for t in sent_prompts)
