"""КТ4, работа 2: предфильтр подписок через кэш Aviasales (Travelpayouts Data API), флаг SUB_PREFILTER.

Флаг SUB_PREFILTER (по умолчанию 0) и TP_TOKEN. Перед сбором подписки на телефонах — один запрос в кэш
(fetch_travelpayouts владельца; в тестах подменён, сеть не трогаем):
- цена кэша (после фильтров подписки) отличается от последней собранной цены подписки на ≥ SUB_PREFILTER_PCT
  (по умолчанию 5 %) или кэш не знает маршрут — собираем, как сейчас;
- кэш изменения не показывает — сбор пропускаем, срок подписки переносится, как после обычной проверки;
- страховочный полный сбор не реже раза в SUB_PREFILTER_FULL_HOURS (по умолчанию — 2 интервала опроса подписки):
  если до следующей проверки срок истечёт, собираем сейчас, кэш не спрашиваем;
- ошибка, таймаут (TP_PREVIEW_TIMEOUT) или лимит API — собираем, как сейчас; WARNING не чаще раза в 10 минут.
Флаг 0 или нет токена — как без предфильтра; флаг 1 без токена — одна строка WARNING при старте.
Пропуски видны в /admin → «Парсинг», под «Источниками».

Модель (внизу файла): синтетические ряды цен, кэш запаздывает на lag; проверки — по ритму владельца (интервал
подписки, джиттер ±10 % по id, шаг планировщика 5 минут); решения — функциями модуля avia.sub_prefilter.
Изменение цены ≥ 5 % должно быть замечено полным сбором не позже 2 интервалов в ≥ 95 % случаев.
Таблица для отчёта: PYTHONPATH=src python -B tests/test_prefilter.py
"""
from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass
from datetime import date
import logging
import math
import random
import sqlite3
import time
from typing import Any

import httpx
import pytest

from avia import admin_panel, sub_prefilter
from avia.bot import Bot
from avia.config import Settings, load_settings
from avia.criteria import SearchCriteria
from avia.models import Offer, Query, SiteResult
from avia.parsers import travelpayouts as tp_client
from avia.scheduler import Scheduler
from avia.subscriptions import SubscriptionStore, poll_interval_seconds

TP_FETCH = "avia.parsers.travelpayouts.fetch_travelpayouts"
TOKEN = "tp-test-token"
MARKER = "777"
TODAY = date(2026, 12, 1)
FAR = Query(origin="MOW", destination="LED", depart=date(2027, 2, 1))  # > 30 дней: интервал 60 мин
HOUR = 3600.0
DAY = 86400.0
T0 = 1_800_000_000.0
PRICE = 10000
TICK = 300.0  # Scheduler.run_forever: шаг планировщика владельца
LOGGER = "avia.sub_prefilter"
REAL_FETCH = tp_client.fetch_travelpayouts  # до подмены в тестах


class FakeClock:
    def __init__(self, start: float = T0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FakeMonotonic(FakeClock):
    def __init__(self) -> None:
        super().__init__(1000.0)


def cache_offer(price: int, currency: str = "RUB", transfers: int | None = None) -> Offer:
    return Offer(site="aviasales (кэш)", price=price, currency=currency, transfers=transfers, url="https://www.aviasales.ru/x")


class FakeCache:
    """Подменённый fetch_travelpayouts: отдаёт заданные билеты, ошибку или молчит delay секунд."""

    def __init__(self, offers: list[Offer] | None = None, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.offers = list(offers or [])
        self.error = error
        self.delay = delay
        self.calls: list[tuple[Query, str, str]] = []

    def set_prices(self, *prices: int) -> None:
        self.offers = [cache_offer(p) for p in prices]

    async def __call__(self, q: Query, token: str, marker: str = "", limit: int = 10) -> list[Offer]:
        self.calls.append((q, token, marker))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.offers)


class Phones:
    """search_fn планировщика: сбор на телефонах (у бота — _scheduler_search, он пишет attempts и site_runs)."""

    def __init__(self, *offers: Offer) -> None:
        self.offers = list(offers) or [Offer(site="aviasales", price=PRICE, currency="RUB", url="https://a/1")]
        self.calls: list[Query] = []

    def set_price(self, price: int) -> None:
        self.offers = [Offer(site="aviasales", price=price, currency="RUB", url=f"https://a/{price}")]

    async def __call__(self, q: Query) -> list[SiteResult]:
        self.calls.append(q)
        return [SiteResult(site="aviasales", ok=True, offers=list(self.offers))]


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str, markup: dict | None) -> None:
        self.sent.append((chat_id, text))


def make_settings(**flags: Any) -> Settings:
    base: dict[str, Any] = dict(sub_prefilter=True, tp_preview_timeout=0.05)
    base.update(flags)
    return Settings(sites=[], tp_token=base.pop("tp_token", TOKEN), tp_marker=MARKER, **base)


def owner_jitter(sub_id: int) -> float:
    """Джиттер интервала подписки, как в SubscriptionStore.due владельца (сверка — test_model_cadence_as_owner_due)."""
    return ((sub_id * 37) % 21 - 10) / 100.0


def due_after(sub_id: int, depart: date) -> float:
    return poll_interval_seconds(depart, TODAY) * (1.0 + owner_jitter(sub_id))


@dataclass
class World:
    store: SubscriptionStore
    clock: FakeClock
    phones: Phones
    notes: Notes
    cache: FakeCache
    sched: Scheduler
    prefilter: sub_prefilter.SubPrefilter | None


def make_world(tmp_path, monkeypatch, settings: Settings | None = None, *, prefilter: bool = True, mono=None) -> World:
    store = SubscriptionStore(tmp_path / "subs.sqlite")
    clock = FakeClock()
    phones = Phones()
    notes = Notes()
    cache = FakeCache([cache_offer(PRICE)])
    monkeypatch.setattr(TP_FETCH, cache)
    settings = settings if settings is not None else make_settings(sub_prefilter_full_hours=24)
    pf = None
    if prefilter:
        kwargs = {"monotonic": mono} if mono is not None else {}
        pf = sub_prefilter.SubPrefilter(store, settings, started_at=T0, **kwargs)
    sched = Scheduler(store, phones, notes, clock=clock, today=lambda: TODAY, prefilter=pf)
    return World(store, clock, phones, notes, cache, sched, pf)


def subscribe(w: World, criteria: SearchCriteria | None = None, query: Query = FAR) -> Any:
    return w.store.create(chat_id=101, query=query, baggage_mode="any", criteria=criteria or SearchCriteria(), now=w.clock())


async def next_check(w: World, sub_id: int, depart: date = FAR.depart) -> None:
    """Ждём срока подписки (ровно её интервал + 1 с) и делаем проход планировщика."""
    w.clock.advance(math.ceil(due_after(sub_id, depart)) + 1)
    await w.sched.run_once()


def sub_price_rows(w: World, sub_id: int) -> list[tuple[int, float]]:
    with sqlite3.connect(w.store.path) as db:
        return db.execute("SELECT price, observed_at FROM sub_prices WHERE sub_id = ? ORDER BY id", (sub_id,)).fetchall()


async def collected(w: World, criteria: SearchCriteria | None = None) -> Any:
    """Подписка, первый проход: цены за окно страховки нет — полный сбор, кэш не спрашиваем."""
    sub = subscribe(w, criteria)
    await w.sched.run_once()
    assert len(w.phones.calls) == 1 and w.cache.calls == []
    return sub


# --- флаги ---------------------------------------------------------------------------------------------------


@pytest.fixture
def no_dotenv(monkeypatch):
    """load_settings не читает .env владельца: флаги — только из окружения теста."""
    dotenv = pytest.importorskip("dotenv")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)


FLAG_ENV = ("SUB_PREFILTER", "SUB_PREFILTER_PCT", "SUB_PREFILTER_FULL_HOURS", "TP_TOKEN")


@pytest.mark.usefixtures("no_dotenv")
def test_flags_default_off(monkeypatch):
    for name in FLAG_ENV:
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.sub_prefilter, s.sub_prefilter_pct, s.sub_prefilter_full_hours) == (False, 5.0, 0.0)
    d = Settings(sites=[])
    assert (d.sub_prefilter, d.sub_prefilter_pct, d.sub_prefilter_full_hours) == (False, 5.0, 0.0)
    assert not sub_prefilter.prefilter_enabled(s) and not sub_prefilter.missing_token(s)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"SUB_PREFILTER": "1", "SUB_PREFILTER_PCT": "7.5", "SUB_PREFILTER_FULL_HOURS": "3"}, (True, 7.5, 3.0)),
        ({"SUB_PREFILTER": "0", "SUB_PREFILTER_PCT": "abc", "SUB_PREFILTER_FULL_HOURS": "x"}, (False, 5.0, 0.0)),
        ({"SUB_PREFILTER": "yes", "SUB_PREFILTER_PCT": "-1", "SUB_PREFILTER_FULL_HOURS": "-2"}, (False, 5.0, 0.0)),
        ({"SUB_PREFILTER": "1", "SUB_PREFILTER_PCT": "nan", "SUB_PREFILTER_FULL_HOURS": "inf"}, (True, 5.0, 0.0)),
        ({"SUB_PREFILTER": " 1 ", "SUB_PREFILTER_PCT": "0", "SUB_PREFILTER_FULL_HOURS": "0"}, (True, 5.0, 0.0)),
    ],
    ids=["on", "bad-number", "only-0-1", "nan-inf", "zero"],
)
@pytest.mark.usefixtures("no_dotenv")
def test_flags_from_env(monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    s = load_settings()
    assert (s.sub_prefilter, s.sub_prefilter_pct, s.sub_prefilter_full_hours) == expected


@pytest.mark.parametrize("token, flag, enabled, missing", [(TOKEN, True, True, False), ("", True, False, True), (TOKEN, False, False, False), ("", False, False, False)])
def test_enabled_only_with_flag_and_token(token, flag, enabled, missing):
    s = Settings(sites=[], tp_token=token, sub_prefilter=flag)
    assert (sub_prefilter.prefilter_enabled(s), sub_prefilter.missing_token(s)) == (enabled, missing)


@pytest.mark.parametrize("depart, interval", [(date(2027, 2, 1), 3600), (date(2026, 12, 20), 2700), (date(2026, 12, 5), 1800)])
def test_full_default_two_promised_intervals(depart, interval):
    assert poll_interval_seconds(depart, TODAY) == interval  # обещанный интервал опроса — у владельца
    assert sub_prefilter.full_seconds(make_settings(), interval) == 2 * interval
    assert sub_prefilter.full_seconds(make_settings(sub_prefilter_full_hours=3), interval) == 3 * HOUR


# --- флаг выключен или токена нет: как сейчас ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_scheduler_as_now(tmp_path, monkeypatch):
    """Без предфильтра — те же сборы, отметки и цены, что у планировщика владельца; кэш не спрашиваем."""
    worlds = []
    for name in ("owner", "off"):
        d = tmp_path / name
        d.mkdir()
        w = make_world(d, monkeypatch, prefilter=False)
        if name == "owner":  # планировщик, собранный без нового параметра
            w.sched = Scheduler(w.store, w.phones, w.notes, clock=w.clock, today=lambda: TODAY)
        sub = subscribe(w)
        await w.sched.run_once()
        for price in (PRICE, 9000, 9000):
            w.phones.set_price(price)
            await next_check(w, sub.id)
        worlds.append((w, sub))
    (a, sa), (b, sb) = worlds
    assert len(a.phones.calls) == len(b.phones.calls) == 4
    assert a.cache.calls == b.cache.calls == []
    assert [p for p, _ in sub_price_rows(a, sa.id)] == [p for p, _ in sub_price_rows(b, sb.id)] == [PRICE, PRICE, 9000, 9000]
    assert a.store.get(sa.id).last_checked_at == b.store.get(sb.id).last_checked_at
    assert [t for _, t in a.notes.sent] == [t for _, t in b.notes.sent] and len(a.notes.sent) == 1


class SchedulerSpy:
    """Подмена Scheduler в bot.run: запоминает, с чем его собрали."""

    made: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        SchedulerSpy.made.append(kwargs)

    async def run_forever(self) -> None:
        await asyncio.sleep(3600)


async def bot_run_scheduler_kwargs(bot: Bot, monkeypatch) -> dict[str, Any]:
    SchedulerSpy.made = []
    monkeypatch.setattr("avia.bot.Scheduler", SchedulerSpy)

    async def call(method: str, **params: Any) -> Any:
        if method == "getUpdates":
            raise asyncio.CancelledError()
        return {}

    bot.call = call  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await bot.run()
    for task in (bot._scheduler_task, getattr(bot, "_maintenance_task", None)):
        if task is not None:
            task.cancel()
    assert len(SchedulerSpy.made) == 1
    return SchedulerSpy.made[0]


def bot_settings(tmp_path, **flags: Any) -> Settings:
    return Settings(sites=[], tg_token="t", tg_allowed_ids={1}, tg_open=True, storage_dir=tmp_path, **flags)


@pytest.mark.asyncio
async def test_flag_off_bot_without_prefilter(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    bot = Bot(bot_settings(tmp_path, tp_token=TOKEN))  # токен есть (П32), флага нет — как сейчас
    assert bot.sub_prefilter is None
    assert not [r for r in caplog.records if "SUB_PREFILTER" in r.getMessage()]
    kwargs = await bot_run_scheduler_kwargs(bot, monkeypatch)
    assert kwargs.get("prefilter") is None


@pytest.mark.asyncio
async def test_flag_on_without_token_one_warning_line(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    bot = Bot(bot_settings(tmp_path, sub_prefilter=True, tp_token=""))
    lines = [r for r in caplog.records if "SUB_PREFILTER" in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.WARNING]
    assert "TP_TOKEN" in lines[0].getMessage()
    assert bot.sub_prefilter is None
    kwargs = await bot_run_scheduler_kwargs(bot, monkeypatch)
    assert kwargs.get("prefilter") is None
    assert len([r for r in caplog.records if "SUB_PREFILTER" in r.getMessage()]) == 1  # не на каждый проход


@pytest.mark.asyncio
async def test_flag_on_with_token_scheduler_gets_prefilter(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    bot = Bot(bot_settings(tmp_path, sub_prefilter=True, tp_token=TOKEN))
    assert isinstance(bot.sub_prefilter, sub_prefilter.SubPrefilter)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "SUB_PREFILTER" in r.getMessage()]
    kwargs = await bot_run_scheduler_kwargs(bot, monkeypatch)
    assert kwargs.get("prefilter") is bot.sub_prefilter


# --- флаг включён и токен есть: ветки решения -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_without_change_skips_collection(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    sub = await collected(w)

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 1  # телефоны не трогали: строк attempts и site_runs нет
    assert len(w.cache.calls) == 1
    q, token, marker = w.cache.calls[0]
    assert (q, token, marker) == (FAR, TOKEN, MARKER)
    assert w.store.get(sub.id).last_checked_at == w.clock()  # срок перенесён, как после обычной проверки
    assert [p for p, _ in sub_price_rows(w, sub.id)] == [PRICE]  # новых цен нет
    assert w.notes.sent == []
    assert w.prefilter.counts(T0)["skip"] == 1
    await w.sched.run_once()  # срок перенесён: в этот же момент подписка не в очереди
    assert len(w.cache.calls) == 1 and len(w.phones.calls) == 1


@pytest.mark.parametrize(
    "cache_price, collect",
    [(10600, True), (9400, True), (10500, True), (9500, True), (10499, False), (9501, False), (10000, False)],
    ids=["+6%", "-6%", "+5%", "-5%", "+4.99%", "-4.99%", "same"],
)
@pytest.mark.asyncio
async def test_cache_differs_by_pct_collects(tmp_path, monkeypatch, cache_price, collect):
    w = make_world(tmp_path, monkeypatch)
    sub = await collected(w)
    w.cache.set_prices(cache_price, cache_price + 3000)

    await next_check(w, sub.id)

    assert len(w.phones.calls) == (2 if collect else 1)
    assert w.prefilter.counts(T0)["changed" if collect else "skip"] == 1
    assert w.store.get(sub.id).last_checked_at == w.clock()


@pytest.mark.asyncio
async def test_pct_from_settings(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, make_settings(sub_prefilter_full_hours=24, sub_prefilter_pct=10))
    sub = await collected(w)
    w.cache.set_prices(10900)
    await next_check(w, sub.id)
    assert len(w.phones.calls) == 1  # 9 % < 10 %
    w.cache.set_prices(11000)
    await next_check(w, sub.id)
    assert len(w.phones.calls) == 2


@pytest.mark.parametrize(
    "offers, criteria",
    [
        ([], None),  # кэш маршрута не знает
        ([cache_offer(PRICE, transfers=1)], SearchCriteria(max_transfers=0)),  # после фильтров подписки — пусто
        ([cache_offer(PRICE, currency="USD")], None),  # другая валюта — сравнить не с чем
    ],
    ids=["empty", "filtered-out", "other-currency"],
)
@pytest.mark.asyncio
async def test_cache_does_not_know_route_collects(tmp_path, monkeypatch, offers, criteria):
    w = make_world(tmp_path, monkeypatch)
    w.phones = Phones(Offer(site="aviasales", price=PRICE, currency="RUB", transfers=0, url="https://a/1"))
    w.sched.search_fn = w.phones
    sub = await collected(w, criteria)
    w.cache.offers = offers

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 2
    assert w.prefilter.counts(T0)["unknown"] == 1


@pytest.mark.asyncio
async def test_cache_price_after_subscription_filters(tmp_path, monkeypatch):
    """Цена кэша — минимум после фильтров подписки (как строка П32), а не общий минимум маршрута."""
    w = make_world(tmp_path, monkeypatch)
    w.phones = Phones(Offer(site="aviasales", price=PRICE, currency="RUB", transfers=0, url="https://a/1"))
    w.sched.search_fn = w.phones
    sub = await collected(w, SearchCriteria(max_transfers=0))
    w.cache.offers = [cache_offer(7000, transfers=2), cache_offer(PRICE, transfers=0)]  # 7000 — с пересадками

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 1  # прямые — те же 10 000: пропуск


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("токен Travelpayouts не принят (401)"),
        httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=httpx.Request("GET", "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"),
            response=httpx.Response(429),
        ),
        httpx.ConnectError("нет сети"),
    ],
    ids=["401", "429-limit", "no-network"],
)
@pytest.mark.asyncio
async def test_cache_error_collects_as_now(tmp_path, monkeypatch, caplog, error):
    caplog.set_level(logging.INFO)
    w = make_world(tmp_path, monkeypatch)
    sub = await collected(w)
    w.cache.error = error

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 2
    assert w.prefilter.counts(T0)["error"] == 1
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert TOKEN not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_cache_timeout_collects_without_waiting(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, make_settings(sub_prefilter_full_hours=24, tp_preview_timeout=0.05))
    sub = await collected(w)
    w.cache.delay = 5.0

    t = time.monotonic()
    await next_check(w, sub.id)

    assert time.monotonic() - t < 1.0  # ждём кэш не дольше TP_PREVIEW_TIMEOUT
    assert len(w.phones.calls) == 2
    assert w.prefilter.counts(T0)["error"] == 1


@pytest.mark.asyncio
async def test_cache_errors_one_warning_per_10_minutes(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    mono = FakeMonotonic()
    w = make_world(tmp_path, monkeypatch, mono=mono)
    sub = await collected(w)
    w.cache.error = RuntimeError("HTTP 502")

    def warnings() -> int:
        return len([r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING])

    await next_check(w, sub.id)
    for _ in range(3):
        mono.advance(150)
        await next_check(w, sub.id)
    assert warnings() == 1  # 4 ошибки за 7,5 минуты — одна строка
    mono.advance(151)  # с первой строки — 10 минут и 1 секунда
    await next_check(w, sub.id)
    assert warnings() == 2
    assert len(w.phones.calls) == 1 + 5  # каждый раз собирали, как сейчас


@pytest.mark.asyncio
async def test_group_of_two_subscriptions_one_request(tmp_path, monkeypatch):
    """Подписки одного маршрута собираются одним поиском: пропуск — только если кэш молчит для каждой."""
    w = make_world(tmp_path, monkeypatch)
    w.phones = Phones(
        Offer(site="aviasales", price=PRICE, currency="RUB", transfers=1, url="https://a/1"),
        Offer(site="aviasales", price=11000, currency="RUB", transfers=0, url="https://a/2"),
    )
    w.sched.search_fn = w.phones
    a = subscribe(w)
    b = subscribe(w, SearchCriteria(max_transfers=0))
    await w.sched.run_once()
    assert [p for p, _ in sub_price_rows(w, a.id)] == [PRICE] and [p for p, _ in sub_price_rows(w, b.id)] == [11000]
    w.cache.offers = [cache_offer(PRICE, transfers=1), cache_offer(11000, transfers=0)]

    w.clock.advance(math.ceil(max(due_after(a.id, FAR.depart), due_after(b.id, FAR.depart))) + 1)
    await w.sched.run_once()
    assert len(w.phones.calls) == 1 and len(w.cache.calls) == 1  # один запрос в кэш на маршрут
    assert w.store.get(a.id).last_checked_at == w.store.get(b.id).last_checked_at == w.clock()

    w.cache.offers = [cache_offer(PRICE, transfers=1), cache_offer(12000, transfers=0)]  # +9 % только у прямых
    w.clock.advance(math.ceil(max(due_after(a.id, FAR.depart), due_after(b.id, FAR.depart))) + 1)
    await w.sched.run_once()
    assert len(w.phones.calls) == 2
    assert len(sub_price_rows(w, a.id)) == len(sub_price_rows(w, b.id)) == 2  # собранное — обеим, как сейчас


@pytest.mark.asyncio
async def test_change_in_cache_then_notification_as_now(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    sub = await collected(w)
    w.cache.set_prices(9000)
    w.phones.set_price(9000)

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 2
    assert len(w.notes.sent) == 1 and "упала" in w.notes.sent[0][1]


@pytest.mark.asyncio
async def test_subscription_without_collected_price_collects_without_cache(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    w.phones.offers = []  # первый сбор без билетов: цены подписки нет
    sub = await collected(w)
    assert sub_price_rows(w, sub.id) == []

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 2 and w.cache.calls == []
    assert w.prefilter.counts(T0)["full"] == 2


@pytest.mark.asyncio
async def test_price_from_subscribe_moment_counts_as_collected(tmp_path, monkeypatch):
    """Цена с момента подписки (живой поиск, dialog.py) — собранная: первая проверка спрашивает кэш."""
    w = make_world(tmp_path, monkeypatch)
    sub = subscribe(w)
    w.store.record_price(sub.id, PRICE, "RUB", "o1", w.clock())
    w.clock.advance(TICK)

    await w.sched.run_once()

    assert w.phones.calls == [] and len(w.cache.calls) == 1
    assert w.store.get(sub.id).last_checked_at == w.clock()


# --- страховочный полный сбор ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_default_two_intervals_skip_would_overrun(tmp_path, monkeypatch):
    """По умолчанию срок — 2 интервала (120 мин). Подписка id 1 проверяется раз в 63,6 мин (джиттер +6 %):
    пропуск дал бы полный сбор через 127 мин — собираем на первой же проверке, кэш не спрашиваем."""
    w = make_world(tmp_path, monkeypatch, make_settings())
    sub = await collected(w)
    assert sub.id == 1 and owner_jitter(sub.id) == 0.06

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 2 and w.cache.calls == []
    assert w.prefilter.counts(T0)["full"] == 2


@pytest.mark.asyncio
async def test_safety_full_hours_from_settings(tmp_path, monkeypatch):
    """SUB_PREFILTER_FULL_HOURS=3: проверки через 63,6 мин — пропуск, пропуск нельзя (3 × 63,6 > 180) — сбор."""
    w = make_world(tmp_path, monkeypatch, make_settings(sub_prefilter_full_hours=3))
    sub = await collected(w)
    t_full = w.clock()

    await next_check(w, sub.id)
    assert len(w.phones.calls) == 1 and len(w.cache.calls) == 1

    await next_check(w, sub.id)
    assert len(w.phones.calls) == 2 and len(w.cache.calls) == 1  # страховочный: кэш не спрашиваем
    assert w.clock() - t_full <= 3 * HOUR
    assert w.prefilter.counts(T0) == {**zero_counts(), "full": 2, "skip": 1}


def zero_counts() -> dict[str, int]:
    return {"skip": 0, "changed": 0, "threshold": 0, "unknown": 0, "full": 0, "error": 0}


@pytest.mark.parametrize("full_intervals", [2, 3, 4, 6])
@pytest.mark.parametrize("ratio", [0.9, 1.0, 1.1, 1.17])
def test_safety_gap_never_exceeds_full_at_steady_rhythm(full_intervals, ratio):
    """Проверки идут ровно (раз в ratio × интервал): между полными сборами — не больше срока страховки,
    даже если кэш ни разу не показал изменения."""
    interval = 3600.0
    full = full_intervals * interval
    gap = ratio * interval
    t_full, last_check, t = 0.0, None, 0.0
    fulls = [0.0]
    while t < 30 * DAY:
        t += gap
        window = sub_prefilter.safety_window(full, sub_prefilter.expected_gap(interval, t, last_check))
        last = PRICE if t - t_full <= window else None
        if sub_prefilter.decide(last, PRICE, 5.0).collect:
            fulls.append(t)
            t_full = t
        last_check = t
    gaps = [b - a for a, b in zip(fulls, fulls[1:])]
    assert max(gaps) <= full + 1e-6
    if 2 * gap <= full:
        assert max(gaps) > gap  # и пропуски были


def test_decide_branches():
    assert sub_prefilter.decide(None, PRICE, 5.0) == sub_prefilter.Decision(True, "full")
    assert sub_prefilter.decide(PRICE, None, 5.0) == sub_prefilter.Decision(True, "unknown")
    assert sub_prefilter.decide(PRICE, 10500, 5.0) == sub_prefilter.Decision(True, "changed")
    assert sub_prefilter.decide(PRICE, 10499, 5.0) == sub_prefilter.Decision(False, "skip")
    assert sub_prefilter.decide(PRICE, 9800, 5.0, threshold=9800) == sub_prefilter.Decision(True, "threshold")
    assert sub_prefilter.decide(9900, PRICE, 5.0, threshold=9900) == sub_prefilter.Decision(True, "threshold")
    assert sub_prefilter.decide(PRICE, 9800, 5.0, threshold=9799) == sub_prefilter.Decision(False, "skip")


# --- /admin → «Парсинг»: счётчик пропусков ---------------------------------------------------------------------


class AdminBot(Bot):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        if method in ("sendMessage", "editMessageText"):
            return {"message_id": 10, "chat": {"id": params.get("chat_id")}}
        return {"ok": True}


async def parse_page(bot: AdminBot, period: str = "24h") -> str:
    cq = {"id": "cq", "from": {"id": 1}, "message": {"chat": {"id": 1}, "message_id": 10}, "data": f"adm:parse:{period}"}
    await bot.handle_callback(cq)
    return [p for m, p in bot.calls if m == "editMessageText"][-1]["text"]


@pytest.mark.asyncio
async def test_admin_parse_without_flag_as_now(tmp_path):
    bot = AdminBot(bot_settings(tmp_path, tp_token=TOKEN))
    bot.metrics.record_run("sub", "aviasales", ok=True, partial=False, captcha=False, error=None, offers=5, elapsed=1.0, ts=time.time() - 60)
    ctx = bot._collect_parse_data("24h")
    assert "sub_prefilter_line" not in ctx
    text = await parse_page(bot)
    assert "Подписки: 1" in text and "Предфильтр" not in text
    assert text == admin_panel.truncate_text(admin_panel.build_parse(ctx, "24h"))


@pytest.mark.asyncio
async def test_admin_parse_shows_skips_under_sources(tmp_path, monkeypatch):
    bot = AdminBot(bot_settings(tmp_path, sub_prefilter=True, tp_token=TOKEN, tp_preview_timeout=0.05))
    monkeypatch.setattr(TP_FETCH, FakeCache([cache_offer(PRICE)]))
    bot.metrics.record_run("sub", "aviasales", ok=True, partial=False, captcha=False, error=None, offers=5, elapsed=1.0, ts=time.time() - 60)
    sub = bot.subs.create(chat_id=101, query=FAR, baggage_mode="any", criteria=SearchCriteria(), now=time.time())
    bot.subs.record_price(sub.id, PRICE, "RUB", "o1", time.time())
    for _ in range(2):  # две проверки, кэш молчит
        assert not (await bot.sub_prefilter.check([bot.subs.get(sub.id)], time.time(), TODAY)).collect

    text = await parse_page(bot)

    lines = text.split("\n")
    i = lines.index("<b>Источники:</b>")
    assert lines[i + 1].startswith("Люди: 0 · Подписки: 1 · Фон: 0")
    assert lines[i + 2].startswith("Предфильтр подписок: проверок 2 · пропущено 2 · собрано 0")


def test_admin_line_counts_and_since_start():
    store = None
    pf = sub_prefilter.SubPrefilter(store, make_settings(), started_at=T0)
    for i, reason in enumerate(["skip", "skip", "changed", "unknown", "full", "error"]):
        pf.record(reason, T0 + i)
    line = pf.admin_line(T0 - DAY)
    assert line.startswith("Предфильтр подписок: проверок 6 · пропущено 2 · собрано 4")
    assert "кэш изменился 1" in line and "кэш не знает 1" in line and "страховочный 1" in line and "ошибка кэша 1" in line
    assert "у порога 0" in line
    assert "с запуска" in line  # счётчик в памяти: после перезапуска — с нуля
    assert "с запуска" not in pf.admin_line(T0 + 1)
    assert pf.counts(T0 + 2) == {**zero_counts(), "changed": 1, "unknown": 1, "full": 1, "error": 1}


# --- по ревью: порог человека, лимит API, туда-обратно, отмена, валюта ----------------------------------------


@pytest.mark.parametrize(
    "threshold, cache_price, collect",
    [
        (9800, 9750, True),  # кэш −2,5 % (меньше 5 %), но ниже порога: собираем, уведомление «ниже порога» — сразу
        (12000, PRICE, True),  # порог поставлен выше текущей цены: собранная уже ниже порога — собираем, как сейчас
        (9000, PRICE, False),  # порог ниже и кэша, и собранной — пропуск
    ],
    ids=["cache-at-threshold", "last-below-threshold", "threshold-below-both"],
)
@pytest.mark.asyncio
async def test_threshold_of_person_collects(tmp_path, monkeypatch, threshold, cache_price, collect):
    w = make_world(tmp_path, monkeypatch)
    sub = await collected(w)
    assert w.store.set_threshold(sub.id, 101, threshold)
    w.cache.set_prices(cache_price)
    w.phones.set_price(cache_price)

    await next_check(w, sub.id)

    assert len(w.phones.calls) == (2 if collect else 1)
    assert w.prefilter.counts(T0)["threshold" if collect else "skip"] == 1
    if collect:
        assert len(w.notes.sent) == 1 and "ниже вашего порога" in w.notes.sent[0][1]


@pytest.mark.asyncio
async def test_rate_limit_429_no_cache_requests_for_10_minutes(tmp_path, monkeypatch):
    mono = FakeMonotonic()
    w = make_world(tmp_path, monkeypatch, mono=mono)
    sub = await collected(w)
    w.cache.error = httpx.HTTPStatusError(
        "429 Too Many Requests",
        request=httpx.Request("GET", tp_client.ENDPOINT),
        response=httpx.Response(429),
    )

    await next_check(w, sub.id)
    assert len(w.cache.calls) == 1 and len(w.phones.calls) == 2

    w.cache.error = None
    mono.advance(599)
    await next_check(w, sub.id)
    assert len(w.cache.calls) == 1 and len(w.phones.calls) == 3  # лимит: кэш не спрашиваем, собираем, как сейчас
    mono.advance(2)
    await next_check(w, sub.id)
    assert len(w.cache.calls) == 2 and len(w.phones.calls) == 3  # через 10 минут — снова кэш, изменения нет
    assert w.prefilter.counts(T0)["error"] == 2


RT = Query(origin="MOW", destination="LED", depart=date(2027, 2, 1), return_date=date(2027, 2, 8))
RT_PAYLOAD = {
    "success": True,
    "currency": "rub",
    "data": [
        {"price": PRICE, "airline": "SU", "departure_at": "2027-02-01T07:15:00+03:00", "return_at": "2027-02-08T19:00:00+03:00",
         "transfers": 0, "duration": 180, "link": "/search/MOW0102LED08021"},
    ],
}


@pytest.mark.asyncio
async def test_round_trip_through_real_fetch_without_network(tmp_path, monkeypatch):
    """Туда-обратно: настоящий fetch_travelpayouts и разбор ответа (сеть подменена), return_at в запросе."""
    w = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(TP_FETCH, REAL_FETCH)
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

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, params: dict, headers: dict) -> FakeResponse:
            seen.append((url, params, headers))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    sub = subscribe(w, query=RT)
    await w.sched.run_once()
    assert len(w.phones.calls) == 1 and w.phones.calls[0] == RT

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 1  # кэш за всю поездку = собранной цене: пропуск
    assert len(seen) == 1
    url, params, headers = seen[0]
    assert url == tp_client.ENDPOINT
    assert params["return_at"] == "2027-02-08" and params["one_way"] == "false"
    assert headers == {"X-Access-Token": TOKEN}


@pytest.mark.asyncio
async def test_cancel_during_cache_request_propagates(tmp_path, monkeypatch):
    """Остановка бота посреди запроса в кэш: отмена не превращается в «ошибку кэша» и сбор."""
    w = make_world(tmp_path, monkeypatch, make_settings(sub_prefilter_full_hours=24, tp_preview_timeout=30))
    sub = await collected(w)
    w.cache.delay = 60.0
    w.clock.advance(math.ceil(due_after(sub.id, FAR.depart)) + 1)

    task = asyncio.create_task(w.sched.run_once())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(w.cache.calls) == 1:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(w.cache.calls) == 1 and len(w.phones.calls) == 1
    assert w.prefilter.counts(T0)["error"] == 0


@pytest.mark.asyncio
async def test_cache_min_in_subscription_currency(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    sub = await collected(w)
    w.cache.offers = [cache_offer(100, currency="USD"), cache_offer(PRICE)]

    await next_check(w, sub.id)

    assert len(w.phones.calls) == 1  # рубли кэша = собранной цене: пропуск, доллары не мешают


# --- модель: синтетические ряды цен, кэш с запаздыванием ------------------------------------------------------


@dataclass(frozen=True)
class Series:
    times: list[float]
    prices: list[int]
    big: list[float]  # моменты изменений ≥ 5 %

    def at(self, t: float) -> int:
        return self.prices[max(0, bisect.bisect_right(self.times, t) - 1)]


def make_series(rng: random.Random, days: float, big_every_h: float = 8.0, small_every_h: float = 2.0) -> Series:
    """Цена — ступеньки: мелкие колебания 0,5–4 % раз в ~2 ч, изменения 5–25 % раз в ~8 ч (пуассоновски)."""
    t, p = 0.0, float(PRICE)
    times, prices, big = [0.0], [PRICE], []
    rate = 1 / (big_every_h * HOUR) + 1 / (small_every_h * HOUR)
    p_big = (1 / big_every_h) / (1 / big_every_h + 1 / small_every_h)
    while True:
        t += rng.expovariate(rate)
        if t >= days * DAY:
            return Series(times, prices, big)
        old = round(p)
        if rng.random() < p_big:
            p *= 1 + rng.choice((-1, 1)) * rng.uniform(0.051, 0.25)
        else:
            p *= 1 + rng.choice((-1, 1)) * rng.uniform(0.005, 0.04)
        p = max(1000.0, p)
        if abs(round(p) - old) * 100 >= 5 * old:
            big.append(t)
        times.append(t)
        prices.append(round(p))


def model_check_times(sub_id: int, interval: float, horizon: float, phase: float) -> list[float]:
    """Моменты проверок подписки: шаг планировщика TICK, срок — интервал × (1 + джиттер), как у владельца."""
    due = interval * (1.0 + owner_jitter(sub_id))
    out: list[float] = []
    t = phase
    while t < horizon:
        if not out or t - out[-1] >= due:
            out.append(t)
        t += TICK
    return out


@dataclass(frozen=True)
class ModelResult:
    noticed: int
    changes: int
    collections: int
    checks: int

    @property
    def share(self) -> float:
        return self.noticed / self.changes

    @property
    def saving(self) -> float:
        return 1 - self.collections / self.checks


def model_one(sub_id: int, interval: float, lag: float, seed: int, *, days: float = 7.0, settings: Settings | None = None) -> ModelResult:
    settings = settings or make_settings()
    rng = random.Random(seed)
    series = make_series(rng, days)
    horizon = days * DAY
    full = sub_prefilter.full_seconds(settings, interval)
    pct = settings.sub_prefilter_pct
    collections = [0.0]  # подписка оформлена после живого поиска: цена собрана в 0
    last_price, last_check = series.at(0.0), None
    checks = model_check_times(sub_id, interval, horizon, phase=rng.uniform(1.0, TICK))  # без предфильтра — сбор на каждой
    for t in checks:
        window = sub_prefilter.safety_window(full, sub_prefilter.expected_gap(interval, t, last_check))
        in_window = last_price if t - collections[-1] <= window else None
        if sub_prefilter.decide(in_window, series.at(t - lag), pct).collect:
            collections.append(t)
            last_price = series.at(t)
        last_check = t
    noticed = changes = 0
    for tc in series.big:
        if tc < interval or tc > horizon - 3 * interval:
            continue
        changes += 1
        i = bisect.bisect_left(collections, tc)
        noticed += i < len(collections) and collections[i] - tc <= 2 * interval
    return ModelResult(noticed, changes, len(collections) - 1, len(checks))


def model(interval: float, lag_hours: float, *, seeds: int = 2, settings: Settings | None = None) -> ModelResult:
    """21 подписка (все значения джиттера владельца) × seeds рядов по 7 суток."""
    parts = [
        model_one(sid, interval, lag_hours * HOUR, seed=sid * 1000 + k * 7 + int(interval), settings=settings)
        for sid in range(1, 22)
        for k in range(seeds)
    ]
    return ModelResult(*(sum(getattr(p, f) for p in parts) for f in ("noticed", "changes", "collections", "checks")))


@pytest.mark.asyncio
async def test_model_cadence_as_owner_due(tmp_path):
    """Ритм проверок в модели — как у SubscriptionStore.due владельца: тот же шаг, интервал и джиттер."""
    store = SubscriptionStore(tmp_path / "subs.sqlite")
    subs = [
        store.create(chat_id=i, query=FAR, baggage_mode="any", criteria=SearchCriteria(), now=0.0) for i in range(21)
    ]
    seen: dict[int, list[float]] = {s.id: [] for s in subs}
    t = 0.0
    while t < 12 * HOUR:
        for s in store.due(t, TODAY):
            seen[s.id].append(t)
            store.mark_checked(s.id, t)
        t += TICK
    for s in subs:
        assert seen[s.id] == model_check_times(s.id, 3600.0, 12 * HOUR, phase=0.0)


@pytest.mark.parametrize("lag_hours", [0, 1, 3])
@pytest.mark.parametrize("interval", [1800, 2700, 3600])
def test_model_change_noticed_within_two_intervals(interval, lag_hours):
    r = model(interval, lag_hours)
    assert r.changes >= 300
    assert r.share >= 0.95
    assert 0 < r.collections < r.checks  # сборов меньше, чем проверок без предфильтра


def report() -> None:
    print("Модель: 21 подписка × 2 ряда × 7 суток на класс; изменение ≥ 5 % раз в ~8 ч, колебания < 5 % раз в ~2 ч")
    print("full | интервал | lag | замечено ≤ 2 интервалов | сборов/сутки без → с | экономия")
    for hours in (0.0, 3.0, 4.0):
        settings = make_settings(sub_prefilter_full_hours=hours)
        for interval in (1800, 2700, 3600):
            for lag in (0, 1, 3):
                r = model(interval, lag, settings=settings)
                per_day = 21 * 2 * 7
                label = "2 инт." if not hours else f"{hours:.0f} ч"
                print(
                    f"{label:6s} | {interval // 60} мин | {lag} ч | {r.share * 100:5.1f} % ({r.noticed}/{r.changes}) | "
                    f"{r.checks / per_day:4.1f} → {r.collections / per_day:4.1f} | {r.saving * 100:4.1f} %"
                )


if __name__ == "__main__":
    report()
