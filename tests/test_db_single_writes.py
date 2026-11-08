"""КТ4, работа 1: хватает ли одному процессу busy_timeout и коротких транзакций (ADR-002: один поток или aiosqlite).

Замеры печатаются строкой KT4DB (pytest -s). Пороги в тестах — с запасом на медленную машину:
тест ловит «database is locked» и записи, которые ждут дольше busy_timeout, а не микросекунды.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import random
import sqlite3
import statistics
import threading
import time
import zlib

import pytest

from avia import chat_state, statedb
from avia.criteria import SearchCriteria
from avia.dialog import Session, Step
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult
from avia.store import ResultStore
from avia.users import UserStore

HISTORY_ROWS = 200_000  # ≈ 45 дней ретеншна владельца при ~4,5 тыс. строк в сутки
SITES = ("aviasales", "kupibilet", "yandex_travel", "tutu", "ozon_travel")


def ms(xs: list[float]) -> str:
    xs = sorted(xs)
    p99 = xs[min(len(xs) - 1, int(len(xs) * 0.99))]
    return f"p50={statistics.median(xs) * 1000:.2f}ms p99={p99 * 1000:.2f}ms max={xs[-1] * 1000:.2f}ms n={len(xs)}"


def realistic_snapshot(store: ResultStore, chat_id: int):
    """Выдача живого поиска: 5 сайтов по 15 билетов (history_max_offers_per_site владельца)."""
    q = Query("MOW", "AER", date.today() + timedelta(days=30))
    results = []
    for s in SITES:
        offers = [Offer(s, 5000 + 37 * i, airline=f"Air{i}", depart_time="10:05", arrive_time="14:40",
                        duration="4ч 35м", transfers=i % 3, baggage="23 кг", url=f"https://{s}.example/x?id={i}" * 3,
                        offer_id=f"{s[:2]}{i}", flight_numbers=[f"SU{1000 + i}"], layovers=["IST"] * (i % 2),
                        depart_at="2026-12-08T10:05", arrive_at="2026-12-08T14:40", fetched_at=time.time())
                  for i in range(15)]
        results.append(SiteResult(s, True, offers, device="redmi", fetch_seconds=30.0))
    flat = [r.offers[0] for r in results]
    pool = [o for r in results for o in r.offers]
    return store.put(chat_id, q, results, "any", flat, criteria=SearchCriteria(), pool=pool, shown=5)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    return statedb.ensure_single_db(tmp_path / "state")


def test_chat_state_write_cost(db):
    cs = chat_state.ChatStateStore(db, snapshot_ttl=1800.0)
    store = ResultStore()
    snap = realistic_snapshot(store, 1)
    session = Session(step=Step.DONE, origin="MOW", destination="AER", depart=date.today())
    size = len(chat_state.encode_snapshot(snap))
    packed = len(zlib.compress(chat_state.encode_snapshot(snap).encode(), chat_state.SNAPSHOT_ZLIB_LEVEL))
    costs = []
    for i in range(200):
        snap.shown = i  # каждое сохранение — новая версия: пишется, а не пропускается
        t0 = time.perf_counter()
        cs.save_chat(1, session, snap)
        costs.append(time.perf_counter() - t0)
    print(f"\nKT4DB save_chat: snapshot json={size} B zlib={packed} B {ms(costs)}")
    assert statistics.median(costs) < 0.05


def _fill_history(path, n: int, span_days: float) -> None:
    PriceHistory(path)
    rng = random.Random(7)
    now = time.time()
    rows = []
    for i in range(n):
        obs = now - rng.uniform(0, span_days) * 86400
        rows.append(("MOW", "AER", (date.today() + timedelta(days=rng.randint(1, 90))).isoformat(), None, 1,
                     rng.choice(SITES), 5000 + rng.randint(0, 5000), "RUB", f"o{i % 3000}", obs, f"s{i // 60}"))
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO price_history (origin, destination, depart, return_date, adults, site, price, currency, "
            "offer_id, observed_at, search_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


@pytest.mark.parametrize("span_days", [46.0, 60.0], ids=["daily_prune_1day", "catch_up_prune_15days"])
def test_loop_writes_wait_behind_daily_maintenance(db, span_days):
    """Раз в сутки (и при старте бота) обслуживание — rebuild_route_daily, prune — держит запись в общей базе.

    Цикл событий в это время пишет человека и сессию синхронно, как сторы владельца: замер ожидания.
    46 дней истории — prune (keep_days=45) удаляет один день, как каждые сутки; 60 — догоняет 15 дней.
    """
    _fill_history(db, HISTORY_ROWS, span_days)
    history = PriceHistory(db)
    users = UserStore(db)
    cs = chat_state.ChatStateStore(db, snapshot_ttl=1800.0)
    snap = realistic_snapshot(ResultStore(), 2)
    session = Session(step=Step.DONE)
    done = threading.Event()
    spans: dict[str, float] = {}

    def maintenance():
        for name, fn in (("rebuild_route_daily", history.rebuild_route_daily), ("prune", history.prune)):
            t0 = time.perf_counter()
            fn()
            spans[name] = time.perf_counter() - t0
        done.set()

    t = threading.Thread(target=maintenance)
    waits_users, waits_chat = [], []
    t.start()
    def timed(waits: list[float], write) -> None:
        t0 = time.perf_counter()
        write()
        waits.append(time.perf_counter() - t0)

    i = 0
    while not done.is_set():
        snap.shown = i  # новая версия выдачи: save_chat пишет, а не пропускает
        person = (waits_users, lambda: users.register(10_000 + i))
        chat = (waits_chat, lambda: cs.save_chat(2, session, snap))
        for waits, write in ((person, chat) if i % 2 == 0 else (chat, person)):  # кто первым упрётся в замок
            timed(waits, write)
        i += 1
        time.sleep(0.002)
    t.join()
    print(f"\nKT4DB maintenance on {HISTORY_ROWS} price_history rows over {span_days:.0f} days: "
          + ", ".join(f"{k}={v * 1000:.0f}ms" for k, v in spans.items()))
    print(f"KT4DB loop users.register during maintenance: {ms(waits_users)}")
    print(f"KT4DB loop save_chat during maintenance: {ms(waits_chat)}")
    assert max(waits_users + waits_chat) < statedb.BUSY_TIMEOUT_MS / 1000
