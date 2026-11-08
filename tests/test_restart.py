"""КТ4, работа 1: рестарт без потерь (П33 — вместо прогона B).

Новый объект бота на той же state/ при DB_SINGLE=1 видит все сессии диалога и последние выдачи,
подписки на месте; сессия старше SESSION_TTL_SECONDS и выдача старше TTL ResultStore (1800 с) не
возвращаются. При DB_SINGLE=0 — как у владельца: сессии и выдачи в памяти, файлов четыре.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import date, timedelta
import sqlite3

import pytest

import avia.bot
from avia import chat_state, statedb
from avia.config import Settings
from avia.dialog import Step
from avia.history import PriceHistory
from avia.metrics import MetricsStore
from avia.models import Leg, Offer, OfferAlt, SiteResult
from avia.subscriptions import SubscriptionStore
from avia.users import UserStore
from tests.test_bot_flow import RecordingBot

ADMIN_ID = 1
SNAPSHOT_TTL = 1800.0
DEPART = date.today() + timedelta(days=30)


def offers_for(site: str, n: int) -> list[Offer]:
    out = []
    for i in range(n):
        o = Offer(site, 5000 + 100 * i, airline=f"Air{i}", depart_time="10:00", arrive_time="14:30",
                  duration="4ч 30м", transfers=i % 2, url=f"https://{site}.example/{i}",
                  flight_numbers=[f"SU{100 + i}"], layovers=["IST"] if i % 2 else None,
                  back=Leg(airline="BackAir", depart_time="09:00", arrive_time="12:00") if i == 0 else None,
                  alts=[OfferAlt("other", 5100 + i)] if i == 1 else None)
        out.append(o)
    return out


class FakeSearch:
    """Живой поиск-заглушка: два сайта, по 9 билетов; hold — держит поиск этого человека."""

    def __init__(self) -> None:
        self.hold: set[int] = set()
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, q, s, *, deep: bool = False, **kwargs):
        if q.adults == 3:  # человек «на рестарте»: поиск висит, пока процесс не умрёт
            self.held.set()
            await self.release.wait()
        a = offers_for("site1", 9)
        a[0].cached_age = 42.0  # атрибут, который ставит fresh_cache: не поле dataclass
        return [SiteResult("site1", True, a, device="redmi", fetch_seconds=12.5),
                SiteResult("site2", True, offers_for("site2", 9)),
                SiteResult("site3", False, [], error="капча", captcha=True)]


def msg(uid: int, text: str) -> dict:
    return {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Имя"}, "text": text}


def cq(uid: int, data: str) -> dict:
    return {"id": f"cq_{uid}_{data}", "message": {"chat": {"id": uid, "type": "private"}, "message_id": 100},
            "from": {"id": uid, "first_name": "Имя"}, "data": data}


def texts(bot: RecordingBot, since: int = 0) -> list[str]:
    return [p.get("text", "") for m, p in bot.calls[since:] if m in ("sendMessage", "editMessageText")]


@pytest.fixture
def real_stores(monkeypatch):
    """conftest подменяет сторы бота лямбдами с путём tmp_path/<имя>.sqlite: здесь нужен путь бота."""
    monkeypatch.setattr(avia.bot, "UserStore", UserStore)
    monkeypatch.setattr(avia.bot, "SubscriptionStore", SubscriptionStore)
    monkeypatch.setattr(avia.bot, "PriceHistory", PriceHistory)
    monkeypatch.setattr(avia.bot, "MetricsStore", MetricsStore)
    statedb.forget_ready()


@pytest.fixture
def single(monkeypatch, real_stores, tmp_path):
    monkeypatch.setenv("DB_SINGLE", "1")
    return tmp_path / "state"


def make_bot(state_dir) -> RecordingBot:
    s = Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN_ID}, tg_open=True, storage_dir=state_dir)
    return RecordingBot(s)


async def to_baggage(bot, uid: int, origin="Москва", dest="Сочи") -> str:
    await bot.handle_callback(cq(uid, "menu:go"))
    for text in (origin, dest, DEPART.strftime("%d.%m.%Y")):
        await bot.handle(msg(uid, text))
    d_id = bot.dialog.sessions[uid].dialog_id
    await bot.handle_callback(cq(uid, f"trip:{d_id}:one"))
    return d_id


async def searched(bot, uid: int) -> str:
    d_id = await to_baggage(bot, uid)
    await bot.handle_callback(cq(uid, f"bag:{d_id}:any"))
    return bot.dialog.sessions[uid].dialog_id


async def populate(bot, search: FakeSearch) -> asyncio.Task:
    """12 человек в разных местах диалога; у человека 312 поиск висит в момент рестарта."""
    for uid in (301, 302, 303):
        await searched(bot, uid)
    d_id = await searched(bot, 304)
    await bot.handle_callback(cq(304, f"more:{d_id}"))  # выдача изменена на месте: flat, shown
    d_id = await searched(bot, 305)
    await bot.handle_callback(cq(305, f"full:{d_id}"))  # view = full, flat — полный отчёт
    d_id = await searched(bot, 306)
    await bot.handle_callback(cq(306, f"track:{d_id}"))  # подписка из выдачи
    d_id = await searched(bot, 307)
    await bot.handle_callback(cq(307, f"track:{d_id}"))
    await bot.handle_callback(cq(308, "menu:go"))
    await bot.handle(msg(308, "Москва"))  # шаг DEST
    await bot.handle_callback(cq(309, "menu:go"))
    await bot.handle(msg(309, "Москва"))
    await bot.handle(msg(309, "Сочи"))  # шаг DATE
    await to_baggage(bot, 310)  # шаг BAGGAGE
    d_id = await to_baggage(bot, 311)
    await bot.handle_callback(cq(311, f"pax:{d_id}"))  # пассажиров 2
    d_id = await to_baggage(bot, 312)
    await bot.handle_callback(cq(312, f"pax:{d_id}"))
    await bot.handle_callback(cq(312, f"pax:{d_id}"))  # 3 пассажира: FakeSearch держит поиск
    hung = asyncio.create_task(bot.handle_callback(cq(312, f"bag:{d_id}:any")))
    await search.held.wait()
    return hung


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def subs_rows(state_dir) -> list[tuple]:
    with sqlite3.connect(statedb.db_path(state_dir, "subscriptions")) as db:
        return db.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()


@pytest.mark.asyncio
async def test_restart_keeps_all_sessions_snapshots_and_subscriptions(single, monkeypatch):
    search = FakeSearch()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot(single)
    hung = await populate(bot, search)
    people = sorted(bot.dialog.sessions)
    assert len(people) == 12
    assert bot.dialog.sessions[312].step == Step.SEARCHING
    sessions = {uid: bot.dialog.sessions[uid] for uid in people}
    snaps = {uid: bot.store.get(uid) for uid in people if bot.store.get(uid) is not None}
    assert len(snaps) == 7
    subs_before = subs_rows(single)
    assert len(subs_before) == 2

    after = make_bot(single)  # рестарт: новый объект бота, та же state/

    assert sorted(after.dialog.sessions) == people  # 100 % сессий
    for uid, s in sessions.items():
        got = after.dialog.sessions[uid]
        if uid == 312:  # поиск умер вместе с процессом: не «Поиск ещё идёт» навсегда
            assert got.step == Step.DONE
            assert got == type(s)(**{**vars(s), "step": Step.DONE})
        else:
            assert got == s, uid
    for uid, snap in snaps.items():  # 100 % последних выдач
        got = after.store.get(uid)
        assert got == snap, uid
        assert [o.offer_id for o in got.flat] == [o.offer_id for o in snap.flat]
    assert after.store.get(304).shown == snaps[304].shown > 0
    assert after.store.get(305).view == "full"
    cached = [o for r in after.store.get(301).results for o in r.offers if getattr(o, "cached_age", None)]
    assert [o.cached_age for o in cached] == [42.0]
    assert subs_rows(single) == subs_before  # 0 потерянных подписок
    await stop(hung)


@pytest.mark.asyncio
async def test_person_continues_after_restart(single, monkeypatch):
    search = FakeSearch()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot(single)
    hung = await populate(bot, search)
    d_301 = bot.dialog.sessions[301].dialog_id
    d_306 = bot.dialog.sessions[306].dialog_id

    after = make_bot(single)

    await after.handle(msg(308, "Сочи"))  # продолжает с шага DEST
    assert after.dialog.sessions[308].step == Step.DATE
    mark = len(after.calls)
    await after.handle_callback(cq(301, f"more:{d_301}"))  # кнопка под выдачей до рестарта
    shown = texts(after, mark)
    assert shown and not any("устарели" in t or "устарел" in t for t in shown)
    assert after.store.get(301).shown > bot.store.get(301).shown
    mark = len(after.calls)
    await after.handle(msg(312, "привет"))
    assert not any("Поиск ещё идёт" in t for t in texts(after, mark))
    await after.handle_callback(cq(306, f"track:{d_306}"))  # та же подписка, не вторая
    assert len(subs_rows(single)) == 2
    await stop(hung)


@pytest.mark.asyncio
async def test_old_session_and_snapshot_not_returned(single, monkeypatch):
    search = FakeSearch()
    monkeypatch.setattr("avia.bot.search", search)
    bot = make_bot(single)
    await searched(bot, 301)
    await searched(bot, 302)
    await bot.handle_callback(cq(303, "menu:go"))
    await bot.handle_callback(cq(304, "menu:go"))
    path = statedb.db_path(single, "users")
    ttl = chat_state.SESSION_TTL_SECONDS
    with sqlite3.connect(path) as db:
        db.execute("UPDATE chat_sessions SET updated_at = updated_at - ? WHERE chat_key = '303'", (ttl + 1,))
        db.execute("UPDATE chat_sessions SET updated_at = updated_at - ? WHERE chat_key = '304'", (ttl - 60,))
        db.execute("UPDATE chat_snapshots SET created_at = created_at - ? WHERE chat_key = '302'",
                   (SNAPSHOT_TTL + 1,))

    after = make_bot(single)

    assert 303 not in after.dialog.sessions  # старше TTL — не возвращается
    assert 304 in after.dialog.sessions  # моложе TTL — на месте
    assert after.store.get(302) is None
    assert after.store.get(301) is not None
    with sqlite3.connect(path) as db:  # старые строки чистятся при старте
        keys = {r[0] for r in db.execute("SELECT chat_key FROM chat_sessions")}
        snap_keys = {r[0] for r in db.execute("SELECT chat_key FROM chat_snapshots")}
    assert "303" not in keys and "302" not in snap_keys


@pytest.mark.asyncio
async def test_flag_off_keeps_owner_behaviour(monkeypatch, real_stores, tmp_path):
    monkeypatch.setenv("DB_SINGLE", "0")
    search = FakeSearch()
    monkeypatch.setattr("avia.bot.search", search)
    state = tmp_path / "state"
    bot = make_bot(state)
    await searched(bot, 301)
    d_id = bot.dialog.sessions[301].dialog_id
    await bot.handle_callback(cq(301, f"track:{d_id}"))
    assert getattr(bot, "chat_state", None) is None
    assert type(bot.store) is avia.bot.ResultStore

    after = make_bot(state)

    assert after.dialog.sessions == {}  # как у владельца: сессии в памяти
    assert after.store.get(301) is None
    assert len(subs_rows(state)) == 1  # подписки и при 0 в SQLite
    assert sorted(p.name for p in state.glob("*.sqlite")) == [
        "history.sqlite", "metrics.sqlite", "subscriptions.sqlite", "users.sqlite"]


def test_session_and_snapshot_roundtrip():
    from avia.criteria import SearchCriteria
    from avia.dialog import Session
    from avia.models import Query
    from avia.store import ResultStore

    s = Session(step=Step.THRESHOLD, origin="MOW", destination="AER", depart=DEPART,
                return_date=DEPART + timedelta(days=7), adults=2, baggage_mode="with",
                criteria=SearchCriteria(max_transfers=1, depart_parts=frozenset({"morning", "day"}), hour_cost=500),
                threshold_sub_id=17)
    assert chat_state.decode_session(chat_state.encode_session(s)) == s

    store = ResultStore()
    res = [SiteResult("site1", True, offers_for("site1", 4), page_url="https://x", partial=True)]
    flat = res[0].offers[:2]
    pool = res[0].offers[1:]
    snap = store.put(1, Query("MOW", "AER", DEPART, return_date=DEPART), res, "any", flat,
                     criteria=SearchCriteria(arrive_parts=frozenset({"night"})), pool=pool, shown=2,
                     dropped_baggage=1, dropped_criteria=3)
    got = chat_state.decode_snapshot(chat_state.encode_snapshot(snap))
    assert got == snap
    # одни и те же билеты в results, flat и pool остаются одними объектами, как до рестарта
    assert got.flat[1] is got.pool[0] is got.results[0].offers[1]
