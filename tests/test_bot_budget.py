"""Бюджет поисков на бота в час и лимит новых людей в сутки при TG_OPEN=1 (КТ1, работа 9).

Оба флага по умолчанию выключены (0): поведение бота как раньше.
- SEARCH_BUDGET_PER_HOUR: сверх бюджета человек получает вежливый отказ, его дневной лимит не списывается;
  администраторы (TG_ALLOWED_IDS) бюджет не тратят и в него не упираются.
- NEW_USERS_PER_DAY: сверх лимита новый человек получает вежливый отказ и не регистрируется;
  уже известные люди и администраторы проходят.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from avia.config import Settings, load_settings
from avia.dialog import Step
from avia.models import Offer, SiteResult
from tests.test_bot_flow import RecordingBot

ADMIN = 1
BUDGET_PHRASE = "дневной лимит не списан"
NEW_PEOPLE_PHRASE = "Загляните завтра"


def make_bot(tmp_path, budget: int = 0, new_per_day: int = 0, tg_open: bool = True) -> RecordingBot:
    s = Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN}, tg_open=tg_open, storage_dir=tmp_path)
    # флаги ставим атрибутами, чтобы тест падал по поведению, а не по сигнатуре Settings
    s.search_budget_per_hour = budget
    s.new_users_per_day = new_per_day
    return RecordingBot(s)


def msg(uid: int, text: str = "/start", chat_type: str = "private") -> dict:
    return {
        "chat": {"id": uid, "type": chat_type},
        "from": {"id": uid, "username": f"u{uid}", "first_name": "Имя"},
        "text": text,
    }


def texts_to(bot: RecordingBot, chat_id: int) -> list[str]:
    return [p.get("text", "") for m, p in bot.calls if m == "sendMessage" and p.get("chat_id") == chat_id]


@pytest.fixture
def searches(monkeypatch) -> list:
    calls: list = []

    async def fake_search(query, s, **kwargs):
        calls.append(query)
        return [SiteResult("aviasales", True, [Offer("aviasales", 8000, airline="Победа")])]

    monkeypatch.setattr("avia.bot.search", fake_search)
    return calls


# --- бюджет поисков на бота ---


@pytest.mark.asyncio
async def test_budget_exhausted_polite_refusal_and_personal_limit_not_charged(tmp_path, searches):
    bot = make_bot(tmp_path, budget=2)
    await bot.handle(msg(10, "/price MOW LED 10.12"))
    await bot.handle(msg(11, "/price MOW LED 10.12"))
    assert len(searches) == 2

    bot.calls.clear()
    await bot.handle(msg(12, "/price MOW LED 10.12"))

    assert len(searches) == 2, "сверх бюджета поиск не должен стартовать"
    sent = texts_to(bot, 12)
    assert len(sent) == 1 and BUDGET_PHRASE in sent[0] and "мин" in sent[0]
    assert not any("Ищу билеты" in t for t in sent)
    assert bot.users.get(12).total_searches == 0, "лимит человека не должен списываться"
    assert bot.dialog.sessions[12].step != Step.SEARCHING
    assert 12 not in bot.busy


@pytest.mark.asyncio
async def test_admin_not_limited_and_not_counted_by_budget(tmp_path, searches):
    bot = make_bot(tmp_path, budget=1)
    await bot.handle(msg(ADMIN, "/price MOW LED 10.12"))
    await bot.handle(msg(ADMIN, "/price MOW LED 11.12"))
    await bot.handle(msg(10, "/price MOW LED 10.12"))
    assert len(searches) == 3, "поиски админа не тратят бюджет"

    await bot.handle(msg(11, "/price MOW LED 10.12"))
    await bot.handle(msg(ADMIN, "/price MOW LED 12.12"))
    assert len(searches) == 4, "11 упирается в бюджет, админ проходит"
    assert any(BUDGET_PHRASE in t for t in texts_to(bot, 11))
    assert not any(BUDGET_PHRASE in t for t in texts_to(bot, ADMIN))


@pytest.mark.asyncio
async def test_deep_search_respects_budget(tmp_path, searches):
    bot = make_bot(tmp_path, budget=1)
    await bot.handle(msg(10, "/price MOW LED 10.12"))
    assert len(searches) == 1
    d_id = bot.dialog.sessions[10].dialog_id

    bot.calls.clear()
    cq = {"id": "cq1", "from": {"id": 10}, "data": f"deep:{d_id}", "message": {"chat": {"id": 10}, "message_id": 5}}
    await bot.handle_callback(cq)

    assert len(searches) == 1
    assert any(BUDGET_PHRASE in t for t in texts_to(bot, 10))
    assert bot.users.get(10).total_searches == 1


def test_budget_window_is_rolling_hour(tmp_path):
    bot = make_bot(tmp_path, budget=2)
    t0 = 1_000_000.0
    assert bot._search_budget_wait(10, now=t0) == 0
    bot._note_search_budget(10, now=t0)
    bot._note_search_budget(11, now=t0 + 600)
    assert bot._search_budget_wait(12, now=t0 + 1200) == pytest.approx(2400)
    assert bot._search_budget_wait(ADMIN, now=t0 + 1200) == 0
    assert bot._search_budget_wait(12, now=t0 + 3600) == 0


# --- флаги выключены ---


@pytest.mark.asyncio
async def test_flags_off_no_change(tmp_path, searches):
    bot = make_bot(tmp_path)
    people = list(range(10, 20))
    for uid in people:
        await bot.handle(msg(uid, "/price MOW LED 10.12"))

    assert len(searches) == len(people)
    for uid in people:
        assert bot.users.get(uid).total_searches == 1
        sent = texts_to(bot, uid)
        assert not any(BUDGET_PHRASE in t or NEW_PEOPLE_PHRASE in t for t in sent)


# --- лимит новых людей в сутки ---


@pytest.mark.asyncio
async def test_new_people_limit_refuses_next_new_person_known_passes(tmp_path):
    bot = make_bot(tmp_path, new_per_day=2)
    bot.users.register(9, now=time.time() - 2 * 86400)  # известен со вчерашнего дня, в лимит дня не входит
    await bot.handle(msg(10))
    await bot.handle(msg(11))
    assert bot.users.get(10) and bot.users.get(11)

    bot.calls.clear()
    await bot.handle(msg(12))
    sent = texts_to(bot, 12)
    assert len(sent) == 1 and NEW_PEOPLE_PHRASE in sent[0]
    assert bot.users.get(12) is None

    bot.calls.clear()
    for uid in (10, 9, ADMIN):
        await bot.handle(msg(uid))
        sent = texts_to(bot, uid)
        assert sent and not any(NEW_PEOPLE_PHRASE in t for t in sent), uid
    assert bot.users.get(ADMIN) is not None


@pytest.mark.asyncio
async def test_new_people_limit_on_callback(tmp_path):
    bot = make_bot(tmp_path, new_per_day=1)
    await bot.handle(msg(10))
    bot.calls.clear()
    cq = {"id": "cq2", "from": {"id": 13}, "data": "menu:go", "message": {"chat": {"id": 13, "type": "private"}, "message_id": 7}}
    await bot.handle_callback(cq)

    assert bot.users.get(13) is None
    answers = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert len(answers) == 1 and NEW_PEOPLE_PHRASE in answers[0].get("text", "")
    assert not [m for m, _ in bot.calls if m != "answerCallbackQuery"]


@pytest.mark.asyncio
async def test_new_people_limit_ignored_when_closed_and_in_groups(tmp_path):
    bot = make_bot(tmp_path, new_per_day=1, tg_open=False)
    await bot.handle(msg(10))
    await bot.handle(msg(11))
    assert not bot.calls and bot.users.get(11) is None

    bot = make_bot(tmp_path / "open", new_per_day=1)
    await bot.handle(msg(10))
    bot.calls.clear()
    await bot.handle(msg(14, chat_type="supergroup"))
    assert not bot.calls and bot.users.get(14) is None


# --- лимит новых людей: без синхронного SQLite в цикле событий ---


def spy_lookups(bot: RecordingBot, monkeypatch) -> list[tuple[str, int]]:
    """Подменяет users.get и users.count_since_created: пишет (имя, id потока) каждого вызова."""
    calls: list[tuple[str, int]] = []
    for name in ("get", "count_since_created"):
        orig = getattr(bot.users, name)

        def wrapper(*args, _orig=orig, _name=name, **kwargs):
            calls.append((_name, threading.get_ident()))
            return _orig(*args, **kwargs)

        monkeypatch.setattr(bot.users, name, wrapper)
    return calls


@pytest.mark.asyncio
async def test_known_person_looked_up_at_most_once(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, new_per_day=5)
    bot.users.register(10, now=time.time() - 2 * 86400)
    calls = spy_lookups(bot, monkeypatch)
    for _ in range(5):
        await bot.handle(msg(10))
    assert len(calls) <= 1, calls

    # человек, которого только что зарегистрировал _admit, тоже больше не ищется в базе
    calls.clear()
    for _ in range(5):
        await bot.handle(msg(11))
    lookups = list(calls)  # до проверки ниже: её users.get тоже попал бы в шпиона
    first_message_lookups = 2  # users.get + счётчик новых людей за сутки
    assert len(lookups) <= first_message_lookups, lookups
    assert bot.users.get(11) is not None


@pytest.mark.asyncio
async def test_unknown_person_lookups_run_off_event_loop(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, new_per_day=1)
    calls = spy_lookups(bot, monkeypatch)
    loop_thread = threading.get_ident()
    await bot.handle(msg(10))
    await bot.handle(msg(12))  # сверх лимита: отказ
    await bot.handle_callback(
        {"id": "cq3", "from": {"id": 13}, "data": "menu:go", "message": {"chat": {"id": 13, "type": "private"}, "message_id": 8}}
    )

    assert {name for name, _ in calls} == {"get", "count_since_created"}
    assert all(tid != loop_thread for _, tid in calls), calls
    assert any(NEW_PEOPLE_PHRASE in t for t in texts_to(bot, 12))
    assert bot.users.get(12) is None and bot.users.get(13) is None


@pytest.mark.asyncio
async def test_new_people_count_cached_per_day(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, new_per_day=3)
    calls = spy_lookups(bot, monkeypatch)
    for uid in (10, 11, 12, 13, 14):
        await bot.handle(msg(uid))

    assert [n for n, _ in calls].count("count_since_created") == 1
    assert all(bot.users.get(uid) for uid in (10, 11, 12))
    assert bot.users.get(13) is None and bot.users.get(14) is None
    assert any(NEW_PEOPLE_PHRASE in t for t in texts_to(bot, 13))


@pytest.mark.asyncio
async def test_new_people_count_resets_next_day(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, new_per_day=1)
    await bot.handle(msg(10))
    await bot.handle(msg(11))
    assert bot.users.get(11) is None

    tomorrow = time.time() + 1  # все уже зарегистрированы «вчера»
    monkeypatch.setattr("avia.bot._local_day_start", lambda ts: tomorrow)
    bot.calls.clear()
    await bot.handle(msg(11))
    assert bot.users.get(11) is not None
    assert not any(NEW_PEOPLE_PHRASE in t for t in texts_to(bot, 11))


@pytest.mark.asyncio
async def test_flag_off_check_makes_no_users_lookups(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.users.register(10)
    calls = spy_lookups(bot, monkeypatch)
    for uid in (10, 10, 11, 12, ADMIN):
        await bot.handle(msg(uid))
    await bot.handle_callback(
        {"id": "cq4", "from": {"id": 13}, "data": "menu:go", "message": {"chat": {"id": 13, "type": "private"}, "message_id": 9}}
    )
    assert calls == []
    assert all(bot.users.get(uid) for uid in (10, 11, 12, 13, ADMIN))


@pytest.mark.asyncio
async def test_concurrent_new_people_do_not_overshoot_limit(tmp_path):
    bot = make_bot(tmp_path, new_per_day=1)
    await asyncio.gather(*(bot.handle(msg(uid)) for uid in (20, 21, 22)))
    admitted = [uid for uid in (20, 21, 22) if bot.users.get(uid) is not None]
    assert len(admitted) == 1, admitted
    refused = [uid for uid in (20, 21, 22) if any(NEW_PEOPLE_PHRASE in t for t in texts_to(bot, uid))]
    assert len(refused) == 2


@pytest.mark.asyncio
async def test_same_new_person_concurrent_messages_take_one_place(tmp_path):
    bot = make_bot(tmp_path, new_per_day=1)
    await asyncio.gather(bot.handle(msg(30)), bot.handle(msg(30, "/help")))
    assert bot.users.get(30) is not None
    assert not any(NEW_PEOPLE_PHRASE in t for t in texts_to(bot, 30))

    await bot.handle(msg(31))
    assert bot.users.get(31) is None
    assert any(NEW_PEOPLE_PHRASE in t for t in texts_to(bot, 31))


# --- настройки ---


def test_settings_defaults_off_and_env(monkeypatch):
    s = Settings(sites=[])
    assert s.search_budget_per_hour == 0
    assert s.new_users_per_day == 0

    monkeypatch.delenv("SEARCH_BUDGET_PER_HOUR", raising=False)
    monkeypatch.delenv("NEW_USERS_PER_DAY", raising=False)
    s = load_settings()
    assert s.search_budget_per_hour == 0 and s.new_users_per_day == 0

    monkeypatch.setenv("SEARCH_BUDGET_PER_HOUR", "60")
    monkeypatch.setenv("NEW_USERS_PER_DAY", "30")
    s = load_settings()
    assert s.search_budget_per_hour == 60 and s.new_users_per_day == 30

    monkeypatch.setenv("SEARCH_BUDGET_PER_HOUR", "много")
    monkeypatch.setenv("NEW_USERS_PER_DAY", "-5")
    s = load_settings()
    assert s.search_budget_per_hour == 0 and s.new_users_per_day == 0
