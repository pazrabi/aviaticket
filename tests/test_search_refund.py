"""П29: суточная попытка поиска возвращается, если поиск не дал человеку цен.

Живой и глубокий поиск списывают попытку (users.try_use_search) до поиска. Если после списания
поиск упал исключением или ни один сайт не ответил (ни у одного SiteResult нет ok, в том числе
после предохранителя и при пустом списке), попытка возвращается — за тот день, в который её
списали, не больше одного раза на поиск. Хоть один сайт ответил, пусть и «рейсов нет», —
попытка остаётся списанной. Ответ из кэша глубокого поиска, отказы «занят» и по лимиту ничего
не списывают и ничего не возвращают. Ошибка базы при возврате не ломает ответ человеку.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
import sqlite3
import time

import pytest

from avia.config import Settings, SiteConfig
from avia.dialog import DeepSearchRequest, SearchRequest, Session, Step
from avia.models import Offer, Query, SiteResult
from avia.users import UserStore
from tests.test_bot_flow import RecordingBot

ADMIN_ID = 1
PERSON_ID = 555
DAILY_LIMIT = 40
SPENT_BEFORE = 3  # попыток человек уже потратил сегодня до поиска в тесте
SITES = ("site1", "site2")
LIMIT_TEXT = "лимит поисков исчерпан"
BUSY_TEXT = "Предыдущий поиск ещё идёт"
QUERY = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 10))
DAY_D, DAY_D1 = date(2026, 11, 8), date(2026, 11, 9)


def _at(day: date, hh: int, mm: int) -> float:
    return datetime(day.year, day.month, day.day, hh, mm).timestamp()


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> list[float]:
    """time.time на полдень дня D: счёт за день не зависит от того, когда идёт прогон."""
    now = [_at(DAY_D, 12, 0)]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


class FlakyBot(RecordingBot):
    """RecordingBot, у которого sendMessage номер fail_send_on (с 1) падает, как сбой Telegram."""

    def __init__(self, settings: Settings, fail_send_on: int | None = None) -> None:
        super().__init__(settings)
        self.fail_send_on = fail_send_on
        self.sends = 0

    async def call(self, method: str, **params):
        if method == "sendMessage":
            self.sends += 1
            if self.sends == self.fail_send_on:
                self.calls.append((method, params))
                raise RuntimeError("telegram sendMessage: Bad Gateway")
        return await super().call(method, **params)


def make_bot(tmp_path, *, fail_send_on: int | None = None, **kw) -> FlakyBot:
    settings = Settings(
        sites=[SiteConfig(name=name, url_template=f"http://{name}/{{origin}}") for name in SITES],
        tg_token="t",
        tg_allowed_ids={ADMIN_ID},
        tg_open=True,
        daily_search_limit=DAILY_LIMIT,
        storage_dir=tmp_path,
        **kw,
    )
    bot = FlakyBot(settings, fail_send_on=fail_send_on)
    for uid in (ADMIN_ID, PERSON_ID):
        bot.users.register(uid, None, "Имя")
    for _ in range(SPENT_BEFORE):
        assert bot.users.try_use_search(PERSON_ID, DAILY_LIMIT)
    return bot


def counters(store: UserStore, uid: int) -> tuple[str | None, int, int]:
    """(search_day, search_count, total_searches) человека прямо из users.sqlite."""
    with sqlite3.connect(store.path) as db:
        row = db.execute(
            "SELECT search_day, search_count, total_searches FROM users WHERE tg_id = ?", (uid,)
        ).fetchone()
    return (row[0], int(row[1]), int(row[2])) if row else (None, 0, 0)


def today_count(bot: FlakyBot, uid: int = PERSON_ID) -> int:
    day, count, _ = counters(bot.users, uid)
    return count if day == datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d") else 0


def sent_texts(bot: FlakyBot) -> list[str]:
    return [p.get("text", "") for m, p in bot.calls if m == "sendMessage"]


def all_down() -> list[SiteResult]:
    return [SiteResult(name, False, [], error="сайт временно недоступен") for name in SITES]


def one_ok_no_flights() -> list[SiteResult]:
    return [SiteResult("site1", True, []), SiteResult("site2", False, [], error="таймаут")]


def with_prices() -> list[SiteResult]:
    return [SiteResult(name, True, [Offer(name, 5000, airline="TestAir")]) for name in SITES]


class FakeSearch:
    """Поиск-заглушка: отдаёт results() или падает; deep — был ли это глубокий поиск."""

    def __init__(self, results=None, *, error: Exception | None = None, before=None) -> None:
        self.results = results
        self.error = error
        self.before = before  # вызывается в начале поиска (сдвиг часов и т.п.)
        self.calls: list[bool] = []

    async def __call__(self, q, s, *, deep: bool = False, **kwargs):
        self.calls.append(deep)
        if self.before is not None:
            self.before()
        if self.error is not None:
            raise self.error
        return self.results()


def live(bot: FlakyBot, uid: int = PERSON_ID):
    bot.dialog.sessions[uid] = Session(step=Step.SEARCHING, dialog_id="d1")
    return bot._run_search(uid, SearchRequest(query=QUERY))


def deep(bot: FlakyBot, uid: int = PERSON_ID):
    bot.store.put(uid, QUERY, with_prices(), "any", [o for r in with_prices() for o in r.offers])
    bot.dialog.sessions[uid] = Session(step=Step.DONE, dialog_id="d1")
    return bot._run_deep_search(uid, DeepSearchRequest(query=QUERY))


# --- UserStore.charge_search / refund_search ---


def test_charge_returns_charge_moment_or_none_over_limit(tmp_path, clock):
    store = UserStore(tmp_path / "u.sqlite")
    store.register(PERSON_ID)

    assert store.charge_search(PERSON_ID, 2) == clock[0]
    assert store.charge_search(PERSON_ID, 2, now=clock[0] + 60) == clock[0] + 60
    assert store.charge_search(PERSON_ID, 2) is None  # лимит 2 исчерпан, ничего не списано

    assert counters(store, PERSON_ID) == ("2026-11-08", 2, 2)


def test_refund_returns_attempt_of_the_charge_day(tmp_path):
    store = UserStore(tmp_path / "u.sqlite")
    store.register(PERSON_ID)
    charged_at = _at(DAY_D, 12, 0)
    for _ in range(3):
        store.try_use_search(PERSON_ID, DAILY_LIMIT, now=charged_at)

    store.refund_search(PERSON_ID, charged_at)

    assert counters(store, PERSON_ID) == ("2026-11-08", 2, 2)


def test_refund_without_user_row_does_nothing(tmp_path):
    store = UserStore(tmp_path / "u.sqlite")

    store.refund_search(PERSON_ID, time.time())

    assert store.get(PERSON_ID) is None
    assert store.count() == 0


def test_refund_never_goes_below_zero(tmp_path):
    store = UserStore(tmp_path / "u.sqlite")
    store.register(PERSON_ID)
    charged_at = _at(DAY_D, 12, 0)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE users SET search_day = '2026-11-08', search_count = 0, total_searches = 0")

    store.refund_search(PERSON_ID, charged_at)

    assert counters(store, PERSON_ID) == ("2026-11-08", 0, 0)


def test_refund_for_previous_day_keeps_new_day_count(tmp_path):
    store = UserStore(tmp_path / "u.sqlite")
    store.register(PERSON_ID)
    charged_at = _at(DAY_D, 23, 59)
    store.try_use_search(PERSON_ID, DAILY_LIMIT, now=charged_at)
    store.try_use_search(PERSON_ID, DAILY_LIMIT, now=_at(DAY_D1, 0, 1))

    store.refund_search(PERSON_ID, charged_at)

    # счёт дня D+1 не тронут; всего поисков — без возвращённого
    assert counters(store, PERSON_ID) == ("2026-11-09", 1, 1)


# --- живой поиск ---


@pytest.mark.asyncio
async def test_live_search_exception_refunds_attempt(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(error=RuntimeError("хаб не отвечает")))

    await live(bot)

    assert today_count(bot) == SPENT_BEFORE
    assert any(t.startswith("⚠️ ") for t in sent_texts(bot))


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [all_down, list], ids=["all_sites_down", "no_results"])
async def test_live_no_site_answered_refunds_attempt(tmp_path, monkeypatch, results):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(results))

    await live(bot)

    assert today_count(bot) == SPENT_BEFORE
    assert not any(t.startswith("⚠️") for t in sent_texts(bot))


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [one_ok_no_flights, with_prices], ids=["ok_no_flights", "prices"])
async def test_live_site_answered_keeps_attempt_charged(tmp_path, monkeypatch, results):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(results))

    await live(bot)

    assert today_count(bot) == SPENT_BEFORE + 1


@pytest.mark.asyncio
async def test_live_safety_fired_with_no_answers_refunds_attempt(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_safety_seconds=0.05)

    async def hanging_search(q, s, **kwargs):
        await asyncio.sleep(5.0)
        return with_prices()

    monkeypatch.setattr("avia.bot.search", hanging_search)

    await live(bot)

    assert today_count(bot) == SPENT_BEFORE
    assert any("пока недоступен" in t for t in sent_texts(bot))


@pytest.mark.asyncio
async def test_live_safety_fired_after_one_answer_keeps_attempt(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_safety_seconds=0.05)

    async def half_search(q, s, on_site_done=None, **kwargs):
        await on_site_done(SiteResult("site1", True, [Offer("site1", 7000, airline="TestAir")]))
        await asyncio.sleep(5.0)
        return with_prices()

    monkeypatch.setattr("avia.bot.search", half_search)

    await live(bot)

    assert today_count(bot) == SPENT_BEFORE + 1


# --- глубокий поиск ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake",
    [lambda: FakeSearch(error=RuntimeError("хаб не отвечает")), lambda: FakeSearch(all_down)],
    ids=["exception", "all_sites_down"],
)
async def test_deep_search_failure_refunds_attempt(tmp_path, monkeypatch, fake):
    bot = make_bot(tmp_path)
    search = fake()
    monkeypatch.setattr("avia.bot.search", search)

    await deep(bot)

    assert search.calls == [True]
    assert today_count(bot) == SPENT_BEFORE


@pytest.mark.asyncio
async def test_deep_search_site_answered_keeps_attempt(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(one_ok_no_flights))

    await deep(bot)

    assert today_count(bot) == SPENT_BEFORE + 1


@pytest.mark.asyncio
async def test_deep_search_from_cache_does_not_touch_count(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    search = FakeSearch(error=RuntimeError("не должен вызываться"))
    monkeypatch.setattr("avia.bot.search", search)
    refunds: list[int] = []
    monkeypatch.setattr(bot.users, "refund_search", lambda uid, *a, **k: refunds.append(uid), raising=False)
    bot.deep_cache.put(QUERY, with_prices())

    await deep(bot)

    assert search.calls == []
    assert today_count(bot) == SPENT_BEFORE
    assert refunds == []
    assert any("из кеша" in t for t in sent_texts(bot))


# --- ошибка базы при возврате ---


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_refund_db_error_keeps_normal_reply(tmp_path, monkeypatch, caplog, run):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(all_down))
    refund_calls: list[int] = []

    def locked(uid, *args, **kwargs):
        refund_calls.append(uid)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bot.users, "refund_search", locked, raising=False)
    caplog.set_level(logging.ERROR, logger="avia.bot")

    await run(bot)

    texts = sent_texts(bot)
    assert not any(t.startswith("⚠️") for t in texts)
    assert len(texts) == 2  # сообщение ожидания и обычная выдача, без новых сообщений
    assert "site1</b>: пока недоступен" in texts[-1]
    assert bot.dialog.sessions[PERSON_ID].step != Step.SEARCHING
    assert PERSON_ID not in bot.busy
    assert refund_calls == [PERSON_ID]
    errors = [
        r for r in caplog.records
        if r.exc_info and r.exc_info[0] is sqlite3.OperationalError and str(PERSON_ID) in r.getMessage()
    ]
    assert len(errors) == 1


# --- стык суток ---


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_refund_across_midnight_keeps_new_day_count(tmp_path, monkeypatch, clock, run):
    """Списали в 23:59 дня D, возврат в 00:01 дня D+1, когда у человека уже есть списание за D+1."""
    bot = make_bot(tmp_path)
    assert counters(bot.users, PERSON_ID) == ("2026-11-08", 3, 3)
    clock[0] = _at(DAY_D, 23, 59)

    def midnight_passes() -> None:
        assert counters(bot.users, PERSON_ID) == ("2026-11-08", 4, 4)  # списали за D
        clock[0] = _at(DAY_D1, 0, 1)
        assert bot.users.try_use_search(PERSON_ID, DAILY_LIMIT)  # списание за D+1

    monkeypatch.setattr("avia.bot.search", FakeSearch(error=RuntimeError("хаб не отвечает"), before=midnight_passes))

    await run(bot)

    # счёт дня D+1 не тронут; всего поисков — без возвращённого
    assert counters(bot.users, PERSON_ID) == ("2026-11-09", 1, 4)


# --- администратор и отказы без списания ---


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_admin_count_stays_zero(tmp_path, monkeypatch, run):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(error=RuntimeError("хаб не отвечает")))
    refunds: list[int] = []
    monkeypatch.setattr(bot.users, "refund_search", lambda uid, *a, **k: refunds.append(uid), raising=False)
    assert counters(bot.users, ADMIN_ID)[1:] == (0, 0)

    await run(bot, ADMIN_ID)

    assert counters(bot.users, ADMIN_ID)[1:] == (0, 0)
    assert refunds == []


@pytest.mark.asyncio
async def test_limit_and_busy_refusals_do_not_refund(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(all_down))
    refunds: list[int] = []
    monkeypatch.setattr(bot.users, "refund_search", lambda uid, *a, **k: refunds.append(uid), raising=False)

    bot.busy.add(PERSON_ID)
    await live(bot)
    await deep(bot)
    bot.busy.discard(PERSON_ID)
    assert today_count(bot) == SPENT_BEFORE

    for _ in range(DAILY_LIMIT - SPENT_BEFORE):
        assert bot.users.try_use_search(PERSON_ID, DAILY_LIMIT)
    await live(bot)
    await deep(bot)

    assert today_count(bot) == DAILY_LIMIT
    assert refunds == []
    texts = sent_texts(bot)
    assert sum(BUSY_TEXT in t for t in texts) == 2
    assert sum(LIMIT_TEXT in t for t in texts) == 2


# --- одно списание — один возврат ---


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_one_charge_one_refund_when_reply_send_fails(tmp_path, monkeypatch, run):
    """Все сайты не ok, потом исключение при отправке выдачи: возврат один, не два."""
    bot = make_bot(tmp_path, fail_send_on=2)  # 1 — сообщение ожидания, 2 — выдача
    monkeypatch.setattr("avia.bot.search", FakeSearch(all_down))

    await run(bot)

    assert today_count(bot) == SPENT_BEFORE
    assert counters(bot.users, PERSON_ID)[2] == SPENT_BEFORE
    assert any(t.startswith("⚠️ ") for t in sent_texts(bot))


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_exception_after_site_answered_refunds_once(tmp_path, monkeypatch, run):
    """Сайты ответили ценами, но выдача не ушла (исключение при отправке): человек получил «⚠️», не цены."""
    bot = make_bot(tmp_path, fail_send_on=2)  # 1 — сообщение ожидания, 2 — выдача
    monkeypatch.setattr("avia.bot.search", FakeSearch(with_prices))

    await run(bot)

    assert today_count(bot) == SPENT_BEFORE
    assert counters(bot.users, PERSON_ID)[2] == SPENT_BEFORE
    assert any(t.startswith("⚠️ ") for t in sent_texts(bot))


# --- отмена ---


@pytest.mark.asyncio
async def test_cancelled_search_refunds_attempt(tmp_path, monkeypatch):
    """Поиск отменили (остановка бота), пока ни один сайт не ответил: попытка возвращается."""
    bot = make_bot(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def hanging_search(q, s, **kwargs):
        started.set()
        await release.wait()
        return with_prices()

    monkeypatch.setattr("avia.bot.search", hanging_search)
    task = asyncio.create_task(live(bot))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0)

    assert today_count(bot) == SPENT_BEFORE
    assert counters(bot.users, PERSON_ID)[2] == SPENT_BEFORE
    assert PERSON_ID not in bot.busy
