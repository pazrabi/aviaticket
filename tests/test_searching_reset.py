"""P0 «Поиск ещё идёт» навсегда: SEARCHING снимается, когда поиск человека закончился.

Кнопка «Искать» (dialog.py, action "bag") ставит SEARCHING до поиска. Если поиск упал или
бот отказал по дневному лимиту, шаг оставался SEARCHING, и на любой текст человек получал
«⏳ Поиск ещё идёт». Отказ «занят» значит, что поиск этого человека уже идёт: шаг он не
трогает, SEARCHING снимает тот, кто держит busy (живой или глубокий поиск), в своём finally.
Исключение в проверках до busy.add (SQLite «database is locked» в дневном лимите) тоже
снимает шаг: человек получает короткую ошибку, исключение — в лог с chat_id.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import avia.users
from avia.config import Settings
from avia.dialog import Step
from avia.models import Offer, SiteResult
from tests.test_bot_flow import RecordingBot

ADMIN_ID = 1
PERSON_ID = 555
STUCK_TEXT = "⏳ Поиск ещё идёт"
BUSY_TEXT = "Предыдущий поиск ещё идёт"
LIMIT_TEXT = "лимит поисков исчерпан"
START_FAILED_TEXT = "⚠️ Не получилось начать поиск, попробуйте ещё раз."
DAY_SECONDS = 86400.0


def make_bot(**kw) -> RecordingBot:
    s = Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN_ID}, tg_open=True, **kw)
    return RecordingBot(s)


class FakeSearch:
    """Поиск-заглушка: пишет вызовы ("live"/"deep"). Вызов номер hold_on ждёт release, fail_on падает."""

    def __init__(self, *, fail_on: int | None = None, hold_on: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.hold_on = hold_on
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, q, s, *, deep: bool = False, **kwargs):
        self.calls.append("deep" if deep else "live")
        n = len(self.calls)
        if n == self.hold_on:
            self.held.set()
            await self.release.wait()
        if n == self.fail_on:
            raise RuntimeError("парсер упал")
        return [SiteResult("site1", True, [Offer("site1", 5000, airline="TestAir")])]


def msg(uid: int, text: str) -> dict:
    return {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Имя"}, "text": text}


def cq(uid: int, data: str) -> dict:
    return {
        "id": f"cq_{data}",
        "message": {"chat": {"id": uid, "type": "private"}, "message_id": 100},
        "from": {"id": uid, "first_name": "Имя"},
        "data": data,
    }


def sent_texts(bot: RecordingBot, since: int = 0) -> list[str]:
    return [p.get("text", "") for m, p in bot.calls[since:] if m == "sendMessage"]


def step_of(bot: RecordingBot, uid: int) -> Step:
    return bot.dialog.sessions[uid].step


async def search_via_dialog(bot: RecordingBot, uid: int) -> None:
    """Человек проходит диалог кнопками и текстом до «✅ Не важно — показать всё»."""
    depart = (date.today() + timedelta(days=30)).strftime("%d.%m.%Y")
    await bot.handle_callback(cq(uid, "menu:go"))
    for text in ("Москва", "Алматы", depart):
        await bot.handle(msg(uid, text))
    d_id = bot.dialog.sessions[uid].dialog_id
    await bot.handle_callback(cq(uid, f"trip:{d_id}:one"))
    assert step_of(bot, uid) == Step.BAGGAGE
    await bot.handle_callback(cq(uid, f"bag:{d_id}:any"))


async def text_replies(bot: RecordingBot, uid: int, text: str = "привет") -> list[str]:
    mark = len(bot.calls)
    await bot.handle(msg(uid, text))
    return sent_texts(bot, mark)


async def assert_not_stuck(bot: RecordingBot, uid: int) -> None:
    """Шаг не SEARCHING, и на обычный текст бот не отвечает «Поиск ещё идёт»."""
    assert step_of(bot, uid) != Step.SEARCHING
    assert not any(STUCK_TEXT in t for t in await text_replies(bot, uid))


@pytest.mark.asyncio
async def test_exception_in_search_resets_searching(monkeypatch):
    """Исключение внутри поиска: ошибка показана, шаг снят, повторный поиск запускается."""
    search = FakeSearch(fail_on=1)
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot()

    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live"]
    assert any(t.startswith("⚠️") for t in sent_texts(bot))
    assert PERSON_ID not in bot.busy
    await assert_not_stuck(bot, PERSON_ID)

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live", "live"]
    assert any("TestAir" in t for t in sent_texts(bot, mark))
    assert step_of(bot, PERSON_ID) == Step.DONE


@pytest.mark.asyncio
async def test_second_press_while_search_running_keeps_searching(monkeypatch):
    """Второе «Искать», пока идёт первый поиск: отказ «занят» шаг не трогает, DONE ставит первый поиск."""
    search = FakeSearch(hold_on=1)
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot()

    first = asyncio.create_task(search_via_dialog(bot, PERSON_ID))
    await search.held.wait()
    d_id = bot.dialog.sessions[PERSON_ID].dialog_id
    mark = len(bot.calls)
    await bot.handle_callback(cq(PERSON_ID, f"bag:{d_id}:any"))
    assert any(BUSY_TEXT in t for t in sent_texts(bot, mark))
    assert step_of(bot, PERSON_ID) == Step.SEARCHING
    assert any(STUCK_TEXT in t for t in await text_replies(bot, PERSON_ID))

    mark = len(bot.calls)
    search.release.set()
    await first
    assert search.calls == ["live"]
    assert any("TestAir" in t for t in sent_texts(bot, mark))
    assert step_of(bot, PERSON_ID) == Step.DONE
    await assert_not_stuck(bot, PERSON_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("deep_fails", [False, True], ids=["deep_ok", "deep_error"])
async def test_press_while_deep_search_running_cleared_by_deep(monkeypatch, deep_fails):
    """«Искать», пока идёт глубокий поиск: отказ «занят», SEARCHING снимает finally глубокого поиска."""
    search = FakeSearch(hold_on=2, fail_on=2 if deep_fails else None)
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot()

    await search_via_dialog(bot, PERSON_ID)
    d_id = bot.dialog.sessions[PERSON_ID].dialog_id
    deep = asyncio.create_task(bot.handle_callback(cq(PERSON_ID, f"deep:{d_id}")))
    await search.held.wait()
    assert search.calls == ["live", "deep"]

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live", "deep"]
    assert any(BUSY_TEXT in t for t in sent_texts(bot, mark))
    assert step_of(bot, PERSON_ID) == Step.SEARCHING
    assert any(STUCK_TEXT in t for t in await text_replies(bot, PERSON_ID))

    search.release.set()
    await deep
    assert PERSON_ID not in bot.busy
    await assert_not_stuck(bot, PERSON_ID)

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live", "deep", "live"]
    assert any("TestAir" in t for t in sent_texts(bot, mark))


@pytest.mark.asyncio
async def test_daily_limit_refusal_resets_searching(monkeypatch):
    """Отказ по дневному лимиту: шаг снят, на следующий день поиск того же человека запускается."""
    search = FakeSearch()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot(daily_search_limit=1)

    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live"]

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live"]
    assert any(LIMIT_TEXT in t for t in sent_texts(bot, mark))
    await assert_not_stuck(bot, PERSON_ID)

    tomorrow = avia.users.time.time() + DAY_SECONDS
    monkeypatch.setattr(avia.users, "time", SimpleNamespace(time=lambda: tomorrow))
    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live", "live"]
    assert any("TestAir" in t for t in sent_texts(bot, mark))


def lock_db_once(monkeypatch, bot: RecordingBot) -> list[tuple]:
    """users.try_use_search: первый вызов — «database is locked», дальше настоящая база."""
    real = bot.users.try_use_search
    calls: list[tuple] = []

    def try_use_search(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(bot.users, "try_use_search", try_use_search)
    return calls


def start_failure_records(caplog, exc_type: type[BaseException]) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "avia.bot" and r.exc_info is not None and isinstance(r.exc_info[1], exc_type)
    ]


@pytest.mark.asyncio
async def test_db_error_in_daily_limit_resets_searching(monkeypatch, caplog):
    """SQLite падает в try_use_search до busy.add: шаг снят, busy пуст, короткая ошибка, лог с chat_id."""
    search = FakeSearch()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot()
    limit_calls = lock_db_once(monkeypatch, bot)
    caplog.set_level(logging.ERROR, logger="avia.bot")

    await search_via_dialog(bot, PERSON_ID)
    assert len(limit_calls) == 1
    assert search.calls == []
    assert PERSON_ID not in bot.busy
    assert START_FAILED_TEXT in sent_texts(bot)
    records = start_failure_records(caplog, sqlite3.OperationalError)
    assert len(records) == 1
    assert str(PERSON_ID) in records[0].getMessage()
    await assert_not_stuck(bot, PERSON_ID)

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert search.calls == ["live"]
    assert any("TestAir" in t for t in sent_texts(bot, mark))
    assert step_of(bot, PERSON_ID) == Step.DONE
