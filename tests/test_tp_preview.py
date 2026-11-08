"""П32, КТ2, работа 10: предварительные цены из кэша Aviasales (Travelpayouts Data API) в сообщении ожидания.

Флаг TP_PREVIEW (по умолчанию 0) и TP_TOKEN. Вместе со стартом живого поиска — отдельная задача запроса кэша
(fetch_travelpayouts владельца; в тестах подменён, сеть не трогаем). Цены пришли не позже TP_PREVIEW_TIMEOUT —
под шапкой сообщения ожидания строка с минимумом кэша после фильтров человека и пометкой «по кэшу Aviasales,
цена может отличаться». Позже, пусто, ошибка — строки нет, поиск идёт как без флага. В конце поиска задача кэша
отменяется. Цены кэша — только в сообщении ожидания: не в карточке, не в снимке выдачи, не в истории цен и не
в метриках сайтов. Колонки searches: preview_seconds, preview_min_price (NULL — предпросмотра не было).

Время сжато, как в test_card_early.py: секунда прода — SCALE реальных секунд (10 мс). Кэш отвечает через
FakeCache.at секунд прода, сайты — через Answer.at.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime
import logging
import sqlite3
import time
from typing import Any

import httpx
import pytest

from avia.bot import Bot
from avia.config import Settings, SiteConfig, load_settings
from avia.criteria import SearchCriteria
from avia.dialog import SearchRequest, Session, Step
from avia.formatting import build_compact_report, money
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.parsers.travelpayouts import parse_prices_for_dates
from avia.tg_text import html_to_plain
from avia.users import UserStore

SCALE = 0.01  # реальных секунд на секунду прода
ADMIN_ID = 1
PERSON_ID = 555
DAILY_LIMIT = 40
DIALOG_ID = "d1"
TOKEN = "tp-test-token"
MARKER = "777"
WAIT_MSG_ID = 301  # первое сообщение поиска — ожидание
QUERY = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
QUERY_RT = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10), return_date=date(2026, 12, 15))
LIVE = ("aviasales", "kupibilet", "tutu", "yandex_travel", "ozon_travel")
NEVER = 10_000.0  # не отвечает до конца поиска
PRICES = {"aviasales": 9000, "kupibilet": 10100, "tutu": 10200, "yandex_travel": 10300, "ozon_travel": 10400}
NOTE = "по кэшу Aviasales, цена может отличаться"
PENDING = "⏳ {} ещё ищет"
TP_FETCH = "avia.parsers.travelpayouts.fetch_travelpayouts"


@dataclass(frozen=True)
class Answer:
    at: float  # секунд прода от старта поиска
    kind: str = "price"  # price — цены; empty — без билетов; error — ошибка сайта


# первый сайт — к 10 с: кэш (1 с) заведомо раньше; aviasales — последний и самый дешёвый
PLAN = {
    "kupibilet": Answer(10),
    "tutu": Answer(12),
    "yandex_travel": Answer(14),
    "ozon_travel": Answer(16),
    "aviasales": Answer(45),
}
# как PLAN_MAIN в test_card_early.py: 4 сайта к 6 с — ранняя карточка П31
PLAN_EARLY = {
    "kupibilet": Answer(3),
    "tutu": Answer(4),
    "yandex_travel": Answer(5),
    "ozon_travel": Answer(6),
    "aviasales": Answer(45),
}
# все сайты к 0,6 с: поиск кончается раньше кэша (2,5 с)
PLAN_FAST = {name: Answer(0.2 + 0.1 * i) for i, name in enumerate(LIVE)}

# ответ prices_for_dates: цена за одного, по возрастанию после разбора
CACHE_PAYLOAD = {
    "success": True,
    "currency": "rub",
    "data": [
        {"price": 8700, "airline": "SU", "departure_at": "2026-12-10T07:15:00+03:00", "duration": 85,
         "transfers": 0, "link": "/search/MOW1012LED1?t=su"},
        {"price": 6900, "airline": "DP", "departure_at": "2026-12-10T23:40:00+03:00", "duration": 300,
         "transfers": 1, "link": "/search/MOW1012LED1?t=dp"},
        {"price": 9900, "airline": "S7", "departure_at": "2026-12-10T13:05:00+03:00", "duration": 90,
         "transfers": 0, "link": "/search/MOW1012LED1?t=s7"},
    ],
}
CACHE = parse_prices_for_dates(CACHE_PAYLOAD, QUERY, MARKER)
CACHE_MIN = 6900  # дешевле всех живых сайтов: если цена кэша попадёт в карточку, её видно
# туда-обратно: duration — сумма плеч (95 + 100 мин), у каждого плеча меньше 2 ч
RT_PAYLOAD = {
    "success": True,
    "currency": "rub",
    "data": [
        {"price": 15400, "airline": "SU", "departure_at": "2026-12-10T07:15:00+03:00",
         "return_at": "2026-12-15T19:40:00+03:00", "transfers": 0, "return_transfers": 0,
         "duration": 195, "duration_to": 95, "duration_back": 100, "link": "/search/MOW1012LED15121?t=rt"},
    ],
}


def site_result(site: str, answer: Answer) -> SiteResult:
    if answer.kind == "price":
        return SiteResult(site, True, [Offer(site, PRICES[site], airline=f"{site}air")], elapsed=answer.at)
    if answer.kind == "empty":
        return SiteResult(site, True, [], elapsed=answer.at)
    return SiteResult(site, False, [], error="таймаут", elapsed=answer.at)


class FakeSearch:
    """pipeline.search: живые сайты разом, on_site_done по готовности, итог — в порядке sites."""

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


class FakeCache:
    """fetch_travelpayouts(q, token, marker, limit): ответ через at секунд прода; error — бросает его."""

    def __init__(self, at: float = 1.0, offers: list[Offer] | None = None, error: Exception | None = None) -> None:
        self.at = at
        self.offers = CACHE if offers is None else offers
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.cancelled = False

    async def __call__(self, q: Query, token: str, marker: str = "", limit: int = 10) -> list[Offer]:
        self.calls.append({"q": q, "token": token, "marker": marker, "limit": limit})
        try:
            await asyncio.sleep(self.at * SCALE)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return [replace(o) for o in self.offers]  # копии: бот не должен править ответ кэша


@dataclass(frozen=True)
class Call:
    method: str
    params: dict[str, Any]
    t: float  # секунд прода от старта _run_search
    busy: bool  # поиск человека ещё держит busy
    ok: bool = True  # False — Telegram ответил ошибкой


class TimedBot(Bot):
    """Пишет вызовы Telegram с моментом и busy. wait_edit_fails — сколько первых правок ожидания падают."""

    def __init__(self, settings: Settings, wait_edit_fails: int = 0) -> None:
        super().__init__(settings)
        self.calls: list[Call] = []
        self.next_msg_id = 300
        self.wait_edit_fails = wait_edit_fails
        self.t0 = 0.0

    async def call(self, method: str, *, retries: int | None = None, **params: Any) -> Any:
        t = (asyncio.get_running_loop().time() - self.t0) / SCALE
        fail = method == "editMessageText" and is_wait(params) and self.wait_edit_fails > 0
        self.calls.append(Call(method, params, t, PERSON_ID in self.busy, ok=not fail))
        if fail:
            self.wait_edit_fails -= 1
            raise RuntimeError("telegram editMessageText: Bad Request: message to edit not found")
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        return True


def make_bot(
    path,
    *,
    preview: bool,
    token: str = TOKEN,
    timeout: float = 3,
    early: bool = False,
    safety: float = 90,
    wait_edit_fails: int = 0,
) -> TimedBot:
    sites = [SiteConfig(name=n, url_template=f"http://{n}/{{origin}}") for n in LIVE]
    flags: dict[str, Any] = {}
    if preview:  # выключенный флаг — без новых полей: так же собирают Settings все, кто о флаге не знает
        flags.update(tp_preview=True, tp_preview_timeout=timeout * SCALE)
    if early:
        flags.update(card_early=True, card_early_seconds=30 * SCALE, card_early_sites=4)
    settings = Settings(
        sites=sites,
        tp_token=token,
        tp_marker=MARKER,
        tg_token="t",
        tg_allowed_ids={ADMIN_ID},
        tg_open=True,
        daily_search_limit=DAILY_LIMIT,
        storage_dir=path,
        live_safety_seconds=safety * SCALE,
        **flags,
    )
    bot = TimedBot(settings, wait_edit_fails=wait_edit_fails)
    # люди и метрики — в path: эталонный бот в своём каталоге не трогает попытку и searches основного
    # (conftest кладёт их в tmp_path теста)
    bot.users = UserStore(path / "users.sqlite")
    bot.metrics = MetricsStore(path / "metrics.sqlite")
    bot.users.register(PERSON_ID, None, "Имя")
    return bot


async def run(
    bot: TimedBot,
    monkeypatch,
    plan: dict[str, Answer],
    cache: FakeCache,
    *,
    query: Query = QUERY,
    baggage_mode: str = "any",
    criteria: SearchCriteria | None = None,
) -> FakeSearch:
    search = FakeSearch(plan)
    monkeypatch.setattr("avia.bot.search", search)
    monkeypatch.setattr(TP_FETCH, cache)
    bot.dialog.sessions[PERSON_ID] = Session(step=Step.SEARCHING, dialog_id=DIALOG_ID)
    bot.t0 = asyncio.get_running_loop().time()
    req = SearchRequest(query=query, baggage_mode=baggage_mode, criteria=criteria or SearchCriteria())
    await bot._run_search(PERSON_ID, req)
    return search


def is_wait(params: dict[str, Any]) -> bool:
    """Правка сообщения ожидания: его id и без клавиатуры (карточка правится с клавиатурой)."""
    return params.get("message_id") == WAIT_MSG_ID and params.get("reply_markup") is None


def is_card(c: Call) -> bool:
    markup = c.params.get("reply_markup") or {}
    return any(b.get("callback_data") == f"track:{DIALOG_ID}" for row in markup.get("inline_keyboard", []) for b in row)


def cards_sent(bot: TimedBot) -> list[Call]:
    return [c for c in bot.calls if c.method == "sendMessage" and is_card(c)]


def card_edits(bot: TimedBot) -> list[Call]:
    return [c for c in bot.calls if c.method == "editMessageText" and is_card(c)]


def card_texts(bot: TimedBot) -> list[str]:
    return [html_to_plain(c.params["text"]) for c in bot.calls if is_card(c)]


def texts(bot: TimedBot) -> list[str]:
    return [html_to_plain(c.params["text"]) for c in bot.calls if c.method in ("sendMessage", "editMessageText")]


def wait_calls(bot: TimedBot) -> list[Call]:
    """Сообщение ожидания: отправка (первый вызов поиска) и его правки."""
    first = bot.calls[0]
    assert first.method == "sendMessage" and not is_card(first)
    return [first] + [c for c in bot.calls if c.method == "editMessageText" and is_wait(c.params)]


def preview_shown(bot: TimedBot) -> list[tuple[float, list[str], int]]:
    """Правки ожидания со строкой кэша: (момент, строки текста, номер строки с пометкой)."""
    out = []
    for c in wait_calls(bot):
        if not c.ok:
            continue
        lines = html_to_plain(c.params["text"]).split("\n")
        hits = [i for i, line in enumerate(lines) if NOTE in line]
        if hits:
            out.append((c.t, lines, hits[0]))
    return out


def calls_log(bot: TimedBot) -> list[tuple[str, dict[str, Any]]]:
    return [(c.method, c.params) for c in bot.calls]


def spy_store(bot: TimedBot) -> list[list[str]]:
    """Снимки выдачи (store.put): какие сайты с ценами в каждом."""
    puts: list[list[str]] = []
    real = bot.store.put

    def put(chat_id, query, results, *args, **kwargs):
        puts.append(sorted(r.site for r in results if r.ok and r.offers))
        return real(chat_id, query, results, *args, **kwargs)

    bot.store.put = put  # type: ignore[method-assign]
    return puts


def spy_history(bot: TimedBot) -> list[list[str]]:
    """Выдачи, что ушли в историю цен (history.annotate): сайты с ценами."""
    seen: list[list[str]] = []
    real = bot.history.annotate

    def annotate(q, results, *args, **kwargs):
        seen.append(sorted(r.site for r in results if r.ok and r.offers))
        return real(q, results, *args, **kwargs)

    bot.history.annotate = annotate  # type: ignore[method-assign]
    return seen


def spy_builds(monkeypatch) -> list[bool]:
    """Сборки карточки: True — ранняя (с pending_sites), False — обычная."""
    import avia.bot

    builds: list[bool] = []
    real = avia.bot.build_compact_report

    def build(*args, **kwargs):
        builds.append("pending_sites" in kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(avia.bot, "build_compact_report", build)
    return builds


def full_chunks(search: FakeSearch, query: Query = QUERY) -> list[str]:
    """Полная карточка по итогу поиска — как её собирает бот (история пуста, фильтров нет)."""
    assert search.results is not None
    chunks, _, _ = build_compact_report(query, search.results, SearchCriteria(), baggage_mode="any")
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


def metric_sites(tmp_path) -> set[str]:
    """Сайты в метриках сайтов: site_runs и attempts."""
    with sqlite3.connect(tmp_path / "metrics.sqlite") as db:
        runs = {r[0] for r in db.execute("SELECT site FROM site_runs")}
        attempts = {r[0] for r in db.execute("SELECT site FROM attempts")}
    return runs | attempts


def new_dir(tmp_path, name: str):
    path = tmp_path / name
    path.mkdir()
    return path


def pending_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


def assert_search_closed(bot: TimedBot) -> None:
    """Конец поиска как в kt2-card: busy пуст, шаг не SEARCHING, попытка списана, ошибки человеку нет."""
    assert PERSON_ID not in bot.busy
    assert bot.dialog.sessions[PERSON_ID].step != Step.SEARCHING
    assert today_count(bot) == 1
    assert not any(t.startswith("⚠️") for t in texts(bot))
    assert bot.calls[-1].method == "deleteMessage" and bot.calls[-1].params["message_id"] == WAIT_MSG_ID
    assert pending_tasks() == []


# --- флаги ---------------------------------------------------------------------------------------------------


@pytest.fixture
def no_dotenv(monkeypatch):
    """load_settings не читает .env владельца: флаги — только из окружения теста."""
    dotenv = pytest.importorskip("dotenv")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)


@pytest.mark.usefixtures("no_dotenv")
def test_flags_default_off(monkeypatch):
    for name in ("TP_PREVIEW", "TP_PREVIEW_TIMEOUT", "TP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.tp_preview, s.tp_preview_timeout) == (False, 3.0)
    d = Settings(sites=[])
    assert (d.tp_preview, d.tp_preview_timeout) == (False, 3.0)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"TP_PREVIEW": "1", "TP_PREVIEW_TIMEOUT": "2.5"}, (True, 2.5)),
        ({"TP_PREVIEW": "0", "TP_PREVIEW_TIMEOUT": "abc"}, (False, 3.0)),
        ({"TP_PREVIEW": "yes", "TP_PREVIEW_TIMEOUT": "-1"}, (False, 3.0)),
        ({"TP_PREVIEW": "1", "TP_PREVIEW_TIMEOUT": "nan"}, (True, 3.0)),
        ({"TP_PREVIEW": " 1 ", "TP_PREVIEW_TIMEOUT": "0"}, (True, 3.0)),
    ],
    ids=["on", "bad-number", "only-0-1", "nan", "zero"],
)
@pytest.mark.usefixtures("no_dotenv")
def test_flags_from_env(monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = load_settings()
    assert (s.tp_preview, s.tp_preview_timeout) == expected


# --- флаг выключен или токена нет: как в kt2-card ------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_no_request_texts_as_kt2_card(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=False)  # токен есть (КТ4), флаг выключен
    puts = spy_store(bot)
    cache = FakeCache()

    search = await run(bot, monkeypatch, PLAN, cache)

    assert cache.calls == []
    assert not any("кэш" in t.lower() for t in texts(bot))
    for c in wait_calls(bot):  # под шапкой (2 строки) — пустая строка, как в kt2-card
        assert c.params["text"].split("\n")[2] == ""
    cards = cards_sent(bot)
    assert [c.params["text"] for c in cards] == full_chunks(search)
    assert cards[0].t >= 45
    assert puts == [sorted(LIVE)]
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_flag_on_without_token_no_request_one_log_line(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    off = make_bot(new_dir(tmp_path, "off"), preview=False)
    await run(off, monkeypatch, PLAN, FakeCache())
    caplog.clear()

    bot = make_bot(tmp_path, preview=True, token="")
    cache = FakeCache()
    await run(bot, monkeypatch, PLAN, cache)

    assert cache.calls == []
    assert calls_log(bot) == calls_log(off)  # те же вызовы Telegram, что с выключенным флагом
    lines = [r.getMessage() for r in caplog.records if "TP_TOKEN" in r.getMessage()]
    assert len(lines) == 1  # при старте бота, не на каждый поиск
    assert_search_closed(bot)


# --- флаг включён и токен есть -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_in_1s_line_under_header_before_first_site(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=True)
    puts = spy_store(bot)
    history = spy_history(bot)
    cache = FakeCache(at=1)

    search = await run(bot, monkeypatch, PLAN, cache)

    # запрос: тот же маршрут, дата, взрослые и валюта (всё в Query), токен и marker владельца
    assert len(cache.calls) == 1
    assert cache.calls[0]["q"] == QUERY
    assert (cache.calls[0]["token"], cache.calls[0]["marker"]) == (TOKEN, MARKER)

    shown = preview_shown(bot)
    assert shown, "строки кэша в сообщении ожидания нет"
    t, lines, i = shown[0]
    assert 1 <= t < PLAN["kupibilet"].at  # раньше ответа первого сайта
    assert "→" in lines[i - 1]  # строка — сразу под шапкой (маршрут и дата)
    line = lines[i]
    assert money(CACHE_MIN, "RUB") in line and "DP" in line and "23:40" in line
    assert lines[i + 1] == ""  # дальше — как было: пустая строка и сайты
    assert all(NOTE in s[1][s[2]] for s in shown)  # пометка — в каждой правке со строкой

    # карточка, снимок, история и метрики сайтов — без кэша
    cards = cards_sent(bot)
    assert [c.params["text"] for c in cards] == full_chunks(search)
    for text in card_texts(bot):
        assert money(CACHE_MIN, "RUB") not in text and "кэш" not in text.lower()
    assert puts == [sorted(LIVE)]
    assert history == [sorted(LIVE)]
    assert metric_sites(tmp_path) == set(LIVE)
    assert_search_closed(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_cache",
    [
        pytest.param(lambda: FakeCache(at=6), id="later-than-timeout"),
        pytest.param(lambda: FakeCache(error=RuntimeError("токен Travelpayouts не принят (401)")), id="401"),
        pytest.param(lambda: FakeCache(error=httpx.ConnectError("nodename nor servname provided")), id="no-network"),
        pytest.param(lambda: FakeCache(error=RuntimeError("API вернул ошибку: success=false")), id="api-error"),
        pytest.param(lambda: FakeCache(offers=[]), id="empty"),
    ],
)
async def test_no_line_when_cache_late_failed_or_empty(tmp_path, monkeypatch, caplog, make_cache):
    caplog.set_level(logging.INFO)
    bot = make_bot(tmp_path, preview=True)
    puts = spy_store(bot)
    cache = make_cache()

    search = await run(bot, monkeypatch, PLAN, cache)

    assert len(cache.calls) == 1
    assert preview_shown(bot) == []
    assert not any("кэш" in t.lower() for t in texts(bot))
    assert [c.params["text"] for c in cards_sent(bot)] == full_chunks(search)  # поиск дошёл до конца
    assert puts == [sorted(LIVE)]
    assert any("кэш" in r.getMessage().lower() for r in caplog.records)  # почему строки нет — в лог
    if cache.at > 3:
        assert cache.cancelled  # поздний ответ не ждём: запрос снят по таймауту
    row = last_search_row(tmp_path)
    assert row["preview_seconds"] is None and row["preview_min_price"] is None
    assert_search_closed(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan, cache_at, timeout, card_from, card_to",
    [
        pytest.param(PLAN, NEVER, NEVER, 45, 90, id="all-sites"),
        pytest.param({**PLAN, "aviasales": Answer(NEVER)}, NEVER, NEVER, 90, 150, id="safety"),
        pytest.param(PLAN_FAST, 2.5, 3, 0.5, 2.5, id="search-faster-than-cache"),
    ],
)
async def test_cache_slower_than_search_cancelled_no_tasks_left(
    tmp_path, monkeypatch, plan, cache_at, timeout, card_from, card_to
):
    bot = make_bot(tmp_path, preview=True, timeout=timeout)
    cache = FakeCache(at=cache_at)

    await run(bot, monkeypatch, plan, cache)

    assert cache.cancelled  # задачу кэша сняли в конце поиска
    assert preview_shown(bot) == []
    cards = cards_sent(bot)
    assert len(cards) == 1 and card_from <= cards[0].t < card_to  # поиск кэш не ждал
    assert_search_closed(bot)  # в том числе висящих задач нет


@pytest.mark.asyncio
async def test_wait_edit_failed_no_line_later(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=True, wait_edit_fails=1)  # правка со строкой кэша упала

    search = await run(bot, monkeypatch, PLAN, FakeCache(at=1))

    failed = [c for c in bot.calls if not c.ok]
    assert len(failed) == 1 and NOTE in html_to_plain(failed[0].params["text"])  # правка со строкой была
    assert preview_shown(bot) == []  # строку не показали — и в правках сайтов её нет
    assert [c.params["text"] for c in cards_sent(bot)] == full_chunks(search)
    row = last_search_row(tmp_path)
    assert row["preview_seconds"] is None and row["preview_min_price"] is None
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_with_card_early_early_card_as_in_kt2_card(tmp_path, monkeypatch):
    plain = make_bot(new_dir(tmp_path, "plain"), preview=False, early=True)
    await run(plain, monkeypatch, PLAN_EARLY, FakeCache())

    bot = make_bot(tmp_path, preview=True, early=True)
    puts = spy_store(bot)
    builds = spy_builds(monkeypatch)
    search = await run(bot, monkeypatch, PLAN_EARLY, FakeCache(at=1))

    shown = preview_shown(bot)
    assert shown and shown[0][0] < PLAN_EARLY["kupibilet"].at
    assert builds == [True, False]  # ранняя с «ещё ищет», полная — обычная
    cards = cards_sent(bot)
    assert len(cards) == 1
    assert 6 <= cards[0].t < 30 and cards[0].busy
    early_text = html_to_plain(cards[0].params["text"])
    assert PENDING.format("Aviasales") in early_text
    assert money(PRICES["kupibilet"], "RUB") in early_text
    edits = card_edits(bot)
    assert len(edits) == 1 and edits[0].t >= 45
    assert edits[0].params["message_id"] == 302  # 301 — ожидание, 302 — карточка
    assert [edits[0].params["text"]] == full_chunks(search)
    for text in card_texts(bot):
        assert money(CACHE_MIN, "RUB") not in text and "кэш" not in text.lower()
    # карточка — те же вызовы Telegram, что без предпросмотра
    assert [(m, p) for m, p in calls_log(bot) if not is_wait(p)] == [
        (m, p) for m, p in calls_log(plain) if not is_wait(p)
    ]
    assert puts == [sorted(LIVE[1:]), sorted(LIVE)]
    row = last_search_row(tmp_path)
    assert row["card_sites_ok"] == 4
    assert row["card_min_price"] == PRICES["kupibilet"]
    assert row["full_min_price"] == PRICES["aviasales"]
    assert row["preview_min_price"] == CACHE_MIN
    assert_search_closed(bot)


# --- фильтры человека ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criteria, price, airline, depart",
    [
        pytest.param(SearchCriteria(max_transfers=0), 8700, "SU", "07:15", id="direct-only"),
        pytest.param(SearchCriteria(depart_parts=frozenset({"day"})), 9900, "S7", "13:05", id="daytime"),
        pytest.param(SearchCriteria(max_duration_hours=2), 8700, "SU", "07:15", id="up-to-2h"),
    ],
)
async def test_line_min_after_person_filters(tmp_path, monkeypatch, criteria, price, airline, depart):
    bot = make_bot(tmp_path, preview=True)

    await run(bot, monkeypatch, PLAN, FakeCache(at=1), criteria=criteria)

    shown = preview_shown(bot)
    assert shown
    _, lines, i = shown[0]
    assert money(price, "RUB") in lines[i] and airline in lines[i] and depart in lines[i]
    assert money(CACHE_MIN, "RUB") not in lines[i]  # самый дешёвый в кэше не прошёл фильтр
    assert last_search_row(tmp_path)["preview_min_price"] == price
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_filters_leave_nothing_in_cache_no_line(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=True)
    night = SearchCriteria(depart_parts=frozenset({"night"}))  # в кэше ночных вылетов нет

    search = await run(bot, monkeypatch, PLAN, FakeCache(at=1), criteria=night)

    assert preview_shown(bot) == []
    assert search.results is not None and len(cards_sent(bot)) == 1  # у живых сайтов время неизвестно — проходят
    row = last_search_row(tmp_path)
    assert row["preview_seconds"] is None and row["preview_min_price"] is None
    assert_search_closed(bot)


@pytest.mark.asyncio
async def test_baggage_not_filtered_cache_has_no_baggage_data(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=True)

    await run(bot, monkeypatch, PLAN, FakeCache(at=1), baggage_mode="with")

    assert all(o.baggage is None for o in CACHE)  # о багаже кэш не знает
    shown = preview_shown(bot)
    assert shown and money(CACHE_MIN, "RUB") in shown[0][1][shown[0][2]]
    assert last_search_row(tmp_path)["preview_min_price"] == CACHE_MIN
    assert_search_closed(bot)


# --- туда-обратно --------------------------------------------------------------------------------------------


def test_query_params_one_way_as_before_round_trip_with_return_at():
    from avia.parsers.travelpayouts import query_params

    one_way = {
        "origin": "MOW",
        "destination": "LED",
        "departure_at": "2026-12-10",
        "one_way": "true",
        "sorting": "price",
        "currency": "rub",
        "limit": 10,
        "page": 1,
    }
    assert query_params(QUERY, 10) == one_way  # в одну сторону — как в kt2-card
    assert query_params(QUERY_RT, 30) == {**one_way, "one_way": "false", "return_at": "2026-12-15", "limit": 30}


@pytest.mark.asyncio
async def test_fetch_travelpayouts_round_trip_without_network(monkeypatch):
    from avia.parsers.travelpayouts import ENDPOINT, fetch_travelpayouts

    seen: list[tuple[str, dict, dict]] = []

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return RT_PAYLOAD

        def raise_for_status(self) -> None:
            return None

    class FakeClient:  # httpx.AsyncClient без сети
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, params: dict, headers: dict) -> FakeResponse:
            seen.append((url, params, headers))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    offers = await fetch_travelpayouts(QUERY_RT, TOKEN, MARKER, limit=30)

    assert len(seen) == 1
    url, params, headers = seen[0]
    assert url == ENDPOINT
    assert params["return_at"] == "2026-12-15" and params["one_way"] == "false"
    assert headers == {"X-Access-Token": TOKEN}
    # ответ туда-обратно разбирает та же parse_prices_for_dates: цена за всю поездку, вылет и авиакомпания «туда»
    assert [(o.price, o.airline, o.depart_time) for o in offers] == [(15400, "SU", "07:15")]


@pytest.mark.asyncio
async def test_round_trip_line_duration_sum_not_taken_for_one_leg(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=True)
    rt_offers = parse_prices_for_dates(RT_PAYLOAD, QUERY_RT, MARKER)
    up_to_2h = SearchCriteria(max_duration_hours=2)  # каждое плечо < 2 ч, сумма — 3 ч 15 мин

    await run(bot, monkeypatch, PLAN, FakeCache(at=1, offers=rt_offers), query=QUERY_RT, criteria=up_to_2h)

    shown = preview_shown(bot)
    assert shown
    _, lines, i = shown[0]
    assert money(15400, "RUB") in lines[i] and "SU" in lines[i] and "07:15" in lines[i]
    for text in card_texts(bot):
        assert money(15400, "RUB") not in text
    assert last_search_row(tmp_path)["preview_min_price"] == 15400
    assert_search_closed(bot)


# --- метрики в searches --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_preview_columns_with_preview(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=True)

    await run(bot, monkeypatch, PLAN, FakeCache(at=1))

    row = last_search_row(tmp_path)
    assert 1 * SCALE <= row["preview_seconds"] < PLAN["kupibilet"].at * SCALE
    assert row["preview_min_price"] == CACHE_MIN
    assert row["full_min_price"] == PRICES["aviasales"]  # расхождение предпросмотра с итогом считается по ним
    assert row["card_seconds"] == row["total_seconds"]  # карточка П31 без флага — как в kt2-card
    assert row["sites_ok"] == 5  # кэш — не сайт поиска


@pytest.mark.asyncio
async def test_metrics_preview_columns_null_without_preview(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, preview=False)

    await run(bot, monkeypatch, PLAN, FakeCache(at=1))

    row = last_search_row(tmp_path)
    assert row["preview_seconds"] is None and row["preview_min_price"] is None
    assert row["card_min_price"] == row["full_min_price"] == PRICES["aviasales"]


@pytest.mark.parametrize("with_card_columns", [True, False], ids=["kt2-card", "before-p31"])
def test_metrics_migration_adds_preview_columns(tmp_path, with_card_columns):
    path = tmp_path / "old_metrics.sqlite"
    card_cols = ", card_seconds REAL, card_sites_ok INTEGER, card_min_price REAL, full_min_price REAL"
    with sqlite3.connect(path) as db:  # searches без preview_* (kt2-card) и без card_* (до П31)
        db.execute(
            "CREATE TABLE searches (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,"
            " total_seconds REAL NOT NULL, first_seconds REAL, sites_total INTEGER NOT NULL,"
            " sites_ok INTEGER NOT NULL, queue_seconds REAL, cls TEXT"
            + (card_cols if with_card_columns else "")
            + ")"
        )
        db.execute(
            "INSERT INTO searches (ts, source, total_seconds, sites_total, sites_ok) VALUES (1.0, 'user', 40.0, 5, 5)"
        )
    store = MetricsStore(path)
    results = [SiteResult("kupibilet", True, [Offer("kupibilet", 10100)], elapsed=6.0)]

    store.record_search("user", 41.0, results, card_seconds=41.0, card_sites_ok=1, card_min_price=10100.0,
                        full_min_price=10100.0, preview_seconds=1.2, preview_min_price=9800.0)
    store.record_search("user", 42.0, results, card_seconds=42.0, card_sites_ok=1, card_min_price=10100.0,
                        full_min_price=10100.0)
    store.record_search("bg", 50.0, results)

    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(searches)")}
        rows = db.execute(
            "SELECT source, preview_seconds, preview_min_price, full_min_price FROM searches ORDER BY id"
        ).fetchall()
    assert {"preview_seconds", "preview_min_price", "card_seconds", "full_min_price"} <= cols
    assert rows == [
        ("user", None, None, None),  # старая строка — NULL
        ("user", 1.2, 9800.0, 10100.0),
        ("user", None, None, 10100.0),  # предпросмотра не было
        ("bg", None, None, None),  # фон и подписки предпросмотр не показывают
    ]
