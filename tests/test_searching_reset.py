"""P0 «Поиск ещё идёт» навсегда: любой выход из Bot._run_search снимает шаг SEARCHING.

Кнопка багажа (dialog.py, action "bag") ставит SEARCHING до поиска. Если поиск упал,
или бот отказал («занят», дневной лимит), шаг оставался SEARCHING, и на любой текст
человек получал «⏳ Поиск ещё идёт». После каждого случая проверяем: шаг не SEARCHING,
текст не упирается в «Поиск ещё идёт», следующий поиск того же человека запускается.
"""
from __future__ import annotations

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
STUCK_TEXT = "Поиск ещё идёт"
BUSY_TEXT = "Предыдущий поиск ещё идёт"
LIMIT_TEXT = "лимит поисков исчерпан"
DAY_SECONDS = 86400.0


def make_bot(**kw) -> RecordingBot:
    s = Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN_ID}, tg_open=True, **kw)
    return RecordingBot(s)


def fake_search(*, fail_first: bool = False):
    """Поиск-заглушка: считает вызовы; с fail_first первый вызов бросает исключение."""
    calls: list[str] = []

    async def _search(q, s, **kwargs):
        calls.append(q.origin)
        if fail_first and len(calls) == 1:
            raise RuntimeError("парсер упал")
        return [SiteResult("site1", True, [Offer("site1", 5000, airline="TestAir")])]

    return _search, calls


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


async def search_via_dialog(bot: RecordingBot, uid: int) -> None:
    """Человек проходит диалог кнопками и текстом до «✅ Не важно — показать всё»."""
    depart = (date.today() + timedelta(days=30)).strftime("%d.%m.%Y")
    await bot.handle_callback(cq(uid, "menu:go"))
    for text in ("Москва", "Алматы", depart):
        await bot.handle(msg(uid, text))
    d_id = bot.dialog.sessions[uid].dialog_id
    await bot.handle_callback(cq(uid, f"trip:{d_id}:one"))
    assert bot.dialog.sessions[uid].step == Step.BAGGAGE
    await bot.handle_callback(cq(uid, f"bag:{d_id}:any"))


async def assert_not_stuck(bot: RecordingBot, uid: int) -> None:
    """Шаг не SEARCHING, и на обычный текст бот не отвечает «Поиск ещё идёт»."""
    session = bot.dialog.sessions.get(uid)
    assert session is not None
    assert session.step != Step.SEARCHING
    mark = len(bot.calls)
    await bot.handle(msg(uid, "привет"))
    assert not any(STUCK_TEXT in t for t in sent_texts(bot, mark))


@pytest.mark.asyncio
async def test_exception_in_search_resets_searching(monkeypatch):
    """Исключение внутри поиска: ошибка показана, шаг снят, повторный поиск запускается."""
    search, calls = fake_search(fail_first=True)
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot()

    await search_via_dialog(bot, PERSON_ID)
    assert len(calls) == 1
    assert any(t.startswith("⚠️") for t in sent_texts(bot))
    assert PERSON_ID not in bot.busy
    await assert_not_stuck(bot, PERSON_ID)

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert len(calls) == 2
    assert any("TestAir" in t for t in sent_texts(bot, mark))
    assert bot.dialog.sessions[PERSON_ID].step == Step.DONE


@pytest.mark.asyncio
async def test_busy_refusal_resets_searching(monkeypatch):
    """Отказ «Предыдущий поиск ещё идёт»: шаг снят, после конца прошлого поиска новый запускается."""
    search, calls = fake_search()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot()
    bot.busy.add(PERSON_ID)  # предыдущий поиск этого человека ещё идёт

    await search_via_dialog(bot, PERSON_ID)
    assert calls == []
    assert any(BUSY_TEXT in t for t in sent_texts(bot))
    await assert_not_stuck(bot, PERSON_ID)

    bot.busy.discard(PERSON_ID)  # предыдущий поиск закончился
    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert len(calls) == 1
    assert any("TestAir" in t for t in sent_texts(bot, mark))


@pytest.mark.asyncio
async def test_daily_limit_refusal_resets_searching(monkeypatch):
    """Отказ по дневному лимиту: шаг снят, на следующий день поиск того же человека запускается."""
    search, calls = fake_search()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot(daily_search_limit=1)

    await search_via_dialog(bot, PERSON_ID)
    assert len(calls) == 1

    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert len(calls) == 1
    assert any(LIMIT_TEXT in t for t in sent_texts(bot, mark))
    await assert_not_stuck(bot, PERSON_ID)

    tomorrow = avia.users.time.time() + DAY_SECONDS
    monkeypatch.setattr(avia.users, "time", SimpleNamespace(time=lambda: tomorrow))
    mark = len(bot.calls)
    await search_via_dialog(bot, PERSON_ID)
    assert len(calls) == 2
    assert any("TestAir" in t for t in sent_texts(bot, mark))
