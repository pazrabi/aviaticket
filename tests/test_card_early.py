"""П31, КТ2, работа 9: ранняя карточка живого поиска под флагом CARD_EARLY (по умолчанию выключен).

Время сжато: секунда прода в тесте — SCALE реальных секунд (10 мс). «Сайт ответил к 10 с» — это 0,1 с настоящего
времени, предохранитель LIVE_SAFETY_SECONDS = 90 с — 0,9 с. Сайт отвечает через Answer.at секунд прода.

Флаг выключен — одна карточка после всех сайтов, как в kt1-all.
Флаг включён — карточка уходит, как только сайтов с ценами не меньше CARD_EARLY_SITES (и кто-то ещё ищет),
или в CARD_EARLY_SECONDS, если к этому моменту цены есть; неответившие — «⏳ <сайт> ещё ищет». Поиск идёт дальше,
в конце та же карточка правится на полную (editMessageText с клавиатурой). Цен к CARD_EARLY_SECONDS нет —
одна карточка в конце, как сейчас.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import sqlite3
import time
from typing import Any

import pytest

from avia.bot import Bot
from avia.config import Settings, SiteConfig, load_settings
from avia.dialog import SearchCriteria, SearchRequest, Session, Step
from avia.formatting import build_compact_report, money
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.tg_text import html_to_plain

SCALE = 0.01  # реальных секунд на секунду прода
ADMIN_ID = 1
PERSON_ID = 555
DAILY_LIMIT = 40
DIALOG_ID = "d1"
QUERY = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
LIVE = ("aviasales", "kupibilet", "tutu", "yandex_travel", "ozon_travel")
TRIAL = "ttravel"  # stage: trial — в живой поиск не идёт (pipeline.search)
NEVER = 10_000.0  # сайт не отвечает до предохранителя
PRICES = {"aviasales": 9000, "kupibilet": 10100, "tutu": 10200, "yandex_travel": 10300, "ozon_travel": 10400}
PENDING = "⏳ {} ещё ищет"
NOT_MODIFIED = (
    "telegram editMessageText: {'ok': False, 'error_code': 400, 'description': "
    "'Bad Request: message is not modified: specified new message content and reply markup are exactly the same'}"
)
NOT_FOUND = (
    "telegram editMessageText: {'ok': False, 'error_code': 400, "
    "'description': 'Bad Request: message to edit not found'}"
)


@dataclass(frozen=True)
class Answer:
    at: float  # секунд прода от старта поиска
    kind: str = "price"  # price — цены; empty — ответил без билетов; error — ошибка сайта


# 4 сайта с ценами к 10 с, aviasales — к 45 с (самый дешёвый: без него минимум карточки дороже)
PLAN_MAIN = {
    "kupibilet": Answer(6),
    "tutu": Answer(7),
    "yandex_travel": Answer(8),
    "ozon_travel": Answer(10),
    "aviasales": Answer(45),
}


def site_result(site: str, answer: Answer) -> SiteResult:
    if answer.kind == "price":
        offers = [Offer(site, PRICES[site], airline=f"{site}air")]
        return SiteResult(site, True, offers, elapsed=answer.at)
    if answer.kind == "empty":
        return SiteResult(site, True, [], elapsed=answer.at)
    return SiteResult(site, False, [], error="таймаут", elapsed=answer.at)


class FakeSearch:
    """pipeline.search: живые сайты (не trial) разом, on_site_done по готовности, итог — в порядке sites."""

    def __init__(self, plan: dict[str, Answer]) -> None:
        self.plan = plan
        self.results: list[SiteResult] | None = None
        self.cancelled = False

    async def __call__(self, q: Query, s: Settings, *, on_site_done: Any = None, **kwargs: Any) -> list[SiteResult]:
        live = [x for x in s.sites if x.enabled and x.stage != "trial"]

        async def one(site: SiteConfig) -> SiteResult:
            answer = self.plan[site.name]
            await asyncio.sleep(answer.at * SCALE)
            res = site_result(site.name, answer)
            if on_site_done is not None:
                await on_site_done(res)
            return res

        try:
            self.results = list(await asyncio.gather(*(one(x) for x in live)))
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.results


@dataclass(frozen=True)
class Call:
    method: str
    params: dict[str, Any]
    t: float  # секунд прода от старта _run_search
    busy: bool  # поиск человека ещё держит busy


class TimedBot(Bot):
    """Пишет вызовы Telegram с моментом и состоянием busy; правка карточки может падать (edit_error)."""

    def __init__(self, settings: Settings, edit_error: str | None = None) -> None:
        super().__init__(settings)
        self.calls: list[Call] = []
        self.next_msg_id = 300
        self.edit_error = edit_error
        self.t0 = 0.0

    async def call(self, method: str, *, retries: int | None = None, **params: Any) -> Any:
        t = (asyncio.get_running_loop().time() - self.t0) / SCALE
        self.calls.append(Call(method, params, t, PERSON_ID in self.busy))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        # правка с клавиатурой — это карточка (сообщение ожидания правится без неё)
        if method == "editMessageText" and self.edit_error and params.get("reply_markup") is not None:
            raise RuntimeError(self.edit_error)
        return True


def make_bot(tmp_path, *, early: bool, trial: bool = False, edit_error: str | None = None) -> TimedBot:
    sites = [SiteConfig(name=n, url_template=f"http://{n}/{{origin}}") for n in LIVE]
    if trial:
        sites.append(SiteConfig(name=TRIAL, url_template=f"http://{TRIAL}/{{origin}}", stage="trial"))
    # выключенный флаг — без новых полей: так же собирают Settings все, кто о флаге не знает
    flags = {"card_early": True, "card_early_seconds": 30 * SCALE, "card_early_sites": 4} if early else {}
    settings = Settings(
        sites=sites,
        tg_token="t",
        tg_allowed_ids={ADMIN_ID},
        tg_open=True,
        daily_search_limit=DAILY_LIMIT,
        storage_dir=tmp_path,
        live_safety_seconds=90 * SCALE,
        **flags,
    )
    bot = TimedBot(settings, edit_error=edit_error)
    bot.users.register(PERSON_ID, None, "Имя")
    return bot


def spy_store(bot: TimedBot) -> list[list[str]]:
    """Снимки выдачи (store.put) по порядку: какие сайты с ценами в каждом."""
    puts: list[list[str]] = []
    real = bot.store.put

    def put(chat_id, query, results, *args, **kwargs):
        puts.append(sorted(r.site for r in results if r.ok and r.offers))
        return real(chat_id, query, results, *args, **kwargs)

    bot.store.put = put  # type: ignore[method-assign]
    return puts


async def run(bot: TimedBot, monkeypatch, plan: dict[str, Answer]) -> FakeSearch:
    search = FakeSearch(plan)
    monkeypatch.setattr("avia.bot.search", search)
    bot.dialog.sessions[PERSON_ID] = Session(step=Step.SEARCHING, dialog_id=DIALOG_ID)
    bot.t0 = asyncio.get_running_loop().time()
    await bot._run_search(PERSON_ID, SearchRequest(query=QUERY))
    return search


def is_card(c: Call) -> bool:
    markup = c.params.get("reply_markup") or {}
    return any(b.get("callback_data") == f"track:{DIALOG_ID}" for row in markup.get("inline_keyboard", []) for b in row)


def cards_sent(bot: TimedBot) -> list[Call]:
    return [c for c in bot.calls if c.method == "sendMessage" and is_card(c)]


def card_edits(bot: TimedBot) -> list[Call]:
    return [c for c in bot.calls if c.method == "editMessageText" and is_card(c)]


def texts(bot: TimedBot) -> list[str]:
    return [html_to_plain(c.params["text"]) for c in bot.calls if c.method in ("sendMessage", "editMessageText")]


def full_chunks(search: FakeSearch) -> list[str]:
    """Полная карточка по итогу поиска — как её собирает бот (история пуста, фильтров нет)."""
    assert search.results is not None
    chunks, _, _ = build_compact_report(QUERY, search.results, SearchCriteria(), baggage_mode="any")
    return chunks


def today_count(bot: TimedBot) -> int:
    with sqlite3.connect(bot.users.path) as db:
        row = db.execute("SELECT search_day, search_count FROM users WHERE tg_id = ?", (PERSON_ID,)).fetchone()
    today = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    return int(row[1]) if row and row[0] == today else 0


def last_search_row(tmp_path) -> sqlite3.Row:
    with sqlite3.connect(tmp_path / "metrics.sqlite") as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM searches ORDER BY id DESC LIMIT 1").fetchone()


def assert_search_closed(bot: TimedBot) -> None:
    """Конец поиска как в kt1-all: busy пуст, шаг не SEARCHING, попытка списана, ошибки человеку нет."""
    assert PERSON_ID not in bot.busy
    assert bot.dialog.sessions[PERSON_ID].step != Step.SEARCHING
    assert today_count(bot) == 1
    assert not any(t.startswith("⚠️") for t in texts(bot))


# --- флаг выключен -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_one_card_after_all_sites_texts_as_now(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=False)
    puts = spy_store(bot)

    search = await run(bot, monkeypatch, PLAN_MAIN)

    cards = cards_sent(bot)
    assert len(cards) == 1
    assert cards[0].t >= 45  # ждала aviasales
    assert card_edits(bot) == []
    assert [cards[0].params["text"]] == full_chunks(search)
    assert not any("ещё ищет" in t for t in texts(bot))
    # сообщение ожидания — первым, убрано после карточки
    assert bot.calls[0].method == "sendMessage" and not is_card(bot.calls[0])
    assert bot.calls[-1].method == "deleteMessage"
    assert bot.calls[-1].params["message_id"] == 301
    assert puts == [sorted(LIVE)]
    assert_search_closed(bot)


@pytest.fixture
def no_dotenv(monkeypatch):
    """load_settings не читает .env владельца: флаги — только из окружения теста."""
    dotenv = pytest.importorskip("dotenv")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)


@pytest.mark.usefixtures("no_dotenv")
def test_flags_default_off(monkeypatch):
    for name in ("CARD_EARLY", "CARD_EARLY_SECONDS", "CARD_EARLY_SITES"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.card_early, s.card_early_seconds, s.card_early_sites) == (False, 30.0, 4)
    d = Settings(sites=[])
    assert (d.card_early, d.card_early_seconds, d.card_early_sites) == (False, 30.0, 4)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"CARD_EARLY": "1", "CARD_EARLY_SECONDS": "25", "CARD_EARLY_SITES": "3"}, (True, 25.0, 3)),
        ({"CARD_EARLY": "0", "CARD_EARLY_SECONDS": "abc", "CARD_EARLY_SITES": "x"}, (False, 30.0, 4)),
        ({"CARD_EARLY": "yes", "CARD_EARLY_SECONDS": "-5", "CARD_EARLY_SITES": "0"}, (False, 30.0, 4)),
    ],
    ids=["on", "bad-numbers", "only-0-1"],
)
@pytest.mark.usefixtures("no_dotenv")
def test_flags_from_env(monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = load_settings()
    assert (s.card_early, s.card_early_seconds, s.card_early_sites) == expected


# --- флаг включён --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_early_card_at_four_sites_then_edit_to_full(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True, trial=True)
    puts = spy_store(bot)

    search = await run(bot, monkeypatch, PLAN_MAIN)

    cards = cards_sent(bot)
    assert len(cards) == 1
    card = cards[0]
    assert 10 <= card.t < 30  # по четырём сайтам, не по времени и не по aviasales
    assert card.busy  # поиск идёт дальше: busy держим до конца, как в kt1-all
    early_text = html_to_plain(card.params["text"])
    assert PENDING.format("Aviasales") in early_text
    assert TRIAL not in early_text  # пробный сайт в живом поиске не участвует — «ещё ищет» про него нет
    assert money(PRICES["kupibilet"], "RUB") in early_text
    assert money(PRICES["aviasales"], "RUB") not in early_text

    edits = card_edits(bot)
    assert len(edits) == 1
    edit = edits[0]
    assert edit.t >= 45
    assert edit.params["message_id"] == 302  # 301 — сообщение ожидания, 302 — карточка
    assert edit.params["parse_mode"] == "HTML"
    assert [edit.params["text"]] == full_chunks(search)
    assert "ещё ищет" not in html_to_plain(edit.params["text"])
    # клавиатура та же: кнопки того же диалога, «Следить» и «Глубокий поиск» на месте
    callbacks = {b["callback_data"] for row in edit.params["reply_markup"]["inline_keyboard"] for b in row}
    assert {f"track:{DIALOG_ID}", f"deep:{DIALOG_ID}", f"new:{DIALOG_ID}"} <= callbacks
    # снимок выдачи: сначала ранний (4 сайта), после правки — полный (5)
    assert puts == [sorted(LIVE[1:]), sorted(LIVE)]
    assert bot.calls[-1].method == "deleteMessage" and bot.calls[-1].params["message_id"] == 301
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_early_card_at_30s_with_two_sites(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)
    puts = spy_store(bot)
    plan = {
        "kupibilet": Answer(5),
        "tutu": Answer(8),
        "aviasales": Answer(45),
        "yandex_travel": Answer(50),
        "ozon_travel": Answer(55),
    }

    search = await run(bot, monkeypatch, plan)

    cards = cards_sent(bot)
    assert len(cards) == 1
    assert 30 <= cards[0].t < 45
    early_text = html_to_plain(cards[0].params["text"])
    for label in ("Aviasales", "Яндекс Путешествия", "Ozon Travel"):
        assert PENDING.format(label) in early_text
    edits = card_edits(bot)
    assert len(edits) == 1 and edits[0].t >= 55
    assert [edits[0].params["text"]] == full_chunks(search)
    assert puts == [["kupibilet", "tutu"], sorted(LIVE)]
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_no_prices_by_30s_one_card_at_end(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)
    puts = spy_store(bot)
    plan = {  # к 30 с цен нет; потом цены у трёх — до четырёх сайтов с ценами не дойти
        "kupibilet": Answer(5, "error"),
        "tutu": Answer(8, "empty"),
        "yandex_travel": Answer(35),
        "ozon_travel": Answer(40),
        "aviasales": Answer(45),
    }

    search = await run(bot, monkeypatch, plan)

    cards = cards_sent(bot)
    assert len(cards) == 1
    assert cards[0].t >= 45
    assert card_edits(bot) == []
    assert [cards[0].params["text"]] == full_chunks(search)
    assert not any("ещё ищет" in t for t in texts(bot))
    assert puts == [["aviasales", "ozon_travel", "yandex_travel"]]
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_all_sites_answered_before_trigger_one_card(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)
    plan = {  # четвёртый сайт с ценами — последний ответивший: ждать некого, карточка одна
        "kupibilet": Answer(5, "error"),
        "tutu": Answer(8),
        "yandex_travel": Answer(10),
        "ozon_travel": Answer(12),
        "aviasales": Answer(20),
    }

    search = await run(bot, monkeypatch, plan)

    cards = cards_sent(bot)
    assert len(cards) == 1 and cards[0].t >= 20
    assert card_edits(bot) == []
    assert [cards[0].params["text"]] == full_chunks(search)
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_safety_after_early_card_edits_to_what_answered(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)
    puts = spy_store(bot)
    plan = {**PLAN_MAIN, "aviasales": Answer(NEVER)}

    search = await run(bot, monkeypatch, plan)

    assert search.cancelled
    cards = cards_sent(bot)
    assert len(cards) == 1 and 10 <= cards[0].t < 30
    edits = card_edits(bot)
    assert len(edits) == 1
    assert 90 <= edits[0].t < 120  # предохранитель 90 с
    full_text = html_to_plain(edits[0].params["text"])
    assert "💤 Aviasales: пока недоступен" in full_text  # «сайт временно недоступен», как сейчас
    assert "ещё ищет" not in full_text
    assert puts == [sorted(LIVE[1:]), sorted(LIVE[1:])]
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_card_edit_not_modified_does_not_break_search(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True, edit_error=NOT_MODIFIED)
    puts = spy_store(bot)

    await run(bot, monkeypatch, PLAN_MAIN)

    assert len(card_edits(bot)) == 1  # правка была и упала
    assert len(cards_sent(bot)) == 1  # «не изменено» — карточка уже такая, второй не шлём
    assert puts == [sorted(LIVE[1:]), sorted(LIVE)]
    assert bot.calls[-1].method == "deleteMessage"
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_card_edit_failed_sends_full_card_anew(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True, edit_error=NOT_FOUND)
    puts = spy_store(bot)

    search = await run(bot, monkeypatch, PLAN_MAIN)

    assert len(card_edits(bot)) == 1
    cards = cards_sent(bot)
    assert len(cards) == 2  # карточку не поправить (её удалили и т. п.) — полная уходит новым сообщением
    assert [cards[1].params["text"]] == full_chunks(search)
    assert puts == [sorted(LIVE[1:]), sorted(LIVE)]
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_full_card_longer_than_one_message_rest_sent_after(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)
    real_build = build_compact_report

    def build(q, results, criteria, **kwargs):
        chunks, flat, pool = real_build(q, results, criteria, **kwargs)
        if kwargs.get("pending_sites"):
            return chunks, flat, pool
        return [*chunks, "хвост полной карточки"], flat, pool  # полная не влезла в одно сообщение

    monkeypatch.setattr("avia.bot.build_compact_report", build)

    search = await run(bot, monkeypatch, PLAN_MAIN)

    edits = card_edits(bot)
    assert len(edits) == 1
    assert [edits[0].params["text"]] == full_chunks(search)
    tail = [c for c in bot.calls if c.method == "sendMessage" and c.params["text"] == "хвост полной карточки"]
    assert len(tail) == 1 and tail[0].t >= edits[0].t
    assert "reply_markup" not in tail[0].params  # клавиатура осталась на карточке
    assert len(cards_sent(bot)) == 1
    assert_search_closed(bot)


# --- метрики в searches --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_flag_off_card_equals_full(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=False)

    await run(bot, monkeypatch, PLAN_MAIN)

    row = last_search_row(tmp_path)
    assert row["card_seconds"] == row["total_seconds"]
    assert row["card_sites_ok"] == row["sites_ok"] == 5
    assert row["card_min_price"] == row["full_min_price"] == PRICES["aviasales"]


@pytest.mark.asyncio
async def test_metrics_flag_on_early_card(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)

    await run(bot, monkeypatch, PLAN_MAIN)

    row = last_search_row(tmp_path)
    assert 10 * SCALE <= row["card_seconds"] < 30 * SCALE
    assert row["total_seconds"] >= 45 * SCALE
    assert row["card_sites_ok"] == 4
    assert row["sites_ok"] == 5
    assert row["card_min_price"] == PRICES["kupibilet"]  # промах: aviasales дешевле, но в карточку не успел
    assert row["full_min_price"] == PRICES["aviasales"]


@pytest.mark.asyncio
async def test_metrics_flag_on_no_early_card_same_as_off(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, early=True)
    plan = {**PLAN_MAIN, "kupibilet": Answer(35), "tutu": Answer(36), "yandex_travel": Answer(37),
            "ozon_travel": Answer(38, "error")}

    await run(bot, monkeypatch, plan)

    row = last_search_row(tmp_path)
    assert row["card_seconds"] == row["total_seconds"]
    assert row["card_sites_ok"] == row["sites_ok"] == 4
    assert row["card_min_price"] == row["full_min_price"] == PRICES["aviasales"]


def test_metrics_migration_adds_card_columns(tmp_path):
    path = tmp_path / "old_metrics.sqlite"
    with sqlite3.connect(path) as db:  # searches до П31: без колонок card_*
        db.execute(
            "CREATE TABLE searches (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,"
            " total_seconds REAL NOT NULL, first_seconds REAL, sites_total INTEGER NOT NULL,"
            " sites_ok INTEGER NOT NULL, queue_seconds REAL, cls TEXT)"
        )
        db.execute(
            "INSERT INTO searches (ts, source, total_seconds, sites_total, sites_ok) VALUES (1.0, 'user', 40.0, 5, 5)"
        )
    store = MetricsStore(path)
    results = [SiteResult("kupibilet", True, [Offer("kupibilet", 10100)], elapsed=6.0)]

    store.record_search("user", 41.0, results, card_seconds=12.5, card_sites_ok=1,
                        card_min_price=10100.0, full_min_price=9000.0)
    store.record_search("bg", 50.0, results)

    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(searches)")}
        rows = db.execute(
            "SELECT source, card_seconds, card_sites_ok, card_min_price, full_min_price FROM searches ORDER BY id"
        ).fetchall()
    assert {"card_seconds", "card_sites_ok", "card_min_price", "full_min_price"} <= cols
    assert rows == [
        ("user", None, None, None, None),  # старая строка — NULL
        ("user", 12.5, 1, 10100.0, 9000.0),
        ("bg", None, None, None, None),  # фон и подписки карточки не шлют
    ]
