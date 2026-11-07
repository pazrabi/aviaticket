"""КТ1, работа 5: тяжёлые запросы к SQLite не держат цикл событий.

История цен (price_history) и метрики (site_runs, attempts) — по 100 тыс. строк.
По 20 вызовов разом: живой поиск (старт каждого сайта идёт через предохранитель
check_site_fuse), выбор кандидатов фона, /stats, история цены (price_curve).

Замер: корутина-таймер спит по 10 мс и запоминает наибольшее опоздание пробуждения.
Пока идут вызовы, цикл не должен стоять дольше 100 мс.

Рядом — что перенос не меняет решений: схема метрик проверяется раз на файл, предохранитель
из потоков пропускает одну пробную попытку, а свёртка выборок в SQLite даёт тот же ответ.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import os
import random
import sqlite3
import statistics
import time

import pytest

from avia.bg_collector import BackgroundCollector
from avia.bot import Bot
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.dialog import HistoryRequest
from avia.dispatcher import check_site_fuse, reset_fuse_state
from avia.guard import SearchGuard
from avia.history import PriceHistory
from avia.metrics import MetricsStore
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import search
from avia.speed import reset_speed_tables

ROWS = 100_000
CALLS = 20
TICK = 0.01
MAX_STALL = 0.1
ATTEMPTS = 3
OFFERS_PER_SEARCH = 25

SITES = ["loop_s1", "loop_s2", "loop_s3", "loop_s4", "loop_s5"]
DEVICES = ["dev1", "dev2", "dev3", "dev4"]
ROUTES = [
    ("MOW", "AER"), ("MOW", "LED"), ("MOW", "KZN"), ("MOW", "SVX"), ("MOW", "OVB"),
    ("LED", "MOW"), ("LED", "AER"), ("KZN", "MOW"), ("SVX", "MOW"), ("OVB", "MOW"),
    ("MOW", "KRR"), ("MOW", "MRV"), ("MOW", "UFA"), ("MOW", "KUF"), ("MOW", "ROV"),
    ("AER", "MOW"), ("KRR", "MOW"), ("UFA", "MOW"), ("KUF", "MOW"), ("ROV", "MOW"),
]
PARSER_NAME = "loop_responsive"


# ============================================================================
# Данные: 100 тыс. строк в каждой таблице, как у бота после нескольких недель работы
# ============================================================================

def _fill_metrics(path, now: float, rng: random.Random) -> None:
    MetricsStore(path)._ensure_schema()
    span = 7 * 86400.0
    runs = []
    attempts = []
    for i in range(ROWS):
        ts = now - span + span * i / ROWS
        site = SITES[i % len(SITES)]
        dev = DEVICES[(i // len(SITES)) % len(DEVICES)]
        ok = rng.random() > 0.1
        err = None if ok else rng.choice(["502 Bad Gateway", "таймаут загрузки страницы"])
        fetch = rng.uniform(5.0, 30.0)
        offers = 10 if ok else 0
        runs.append((ts, "user", site, int(ok), 0, 0, err, offers, fetch, dev, 0.5, fetch, 0))
        attempts.append(
            (ts, f"s{i // len(SITES)}", site, dev, "user", "primary", "ok" if ok else "error", offers, fetch, 0.5)
        )
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO site_runs (ts, source, site, ok, partial, captcha, error, offers, elapsed, "
            "device, queue_wait, fetch_seconds, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            runs,
        )
        db.executemany(
            "INSERT INTO attempts (ts, search_id, site, device, source, role, outcome, offers, "
            "fetch_seconds, queue_wait) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            attempts,
        )


def _fill_history(path, now: float, rng: random.Random) -> None:
    PriceHistory(path)
    span = 30 * 86400.0
    today = date.fromtimestamp(now)
    searches = ROWS // OFFERS_PER_SEARCH
    rows = []
    for k in range(searches):
        obs = now - span + span * k / searches
        origin, destination = ROUTES[k % len(ROUTES)]
        depart = today + timedelta(days=1 + (k * 7) % 90)
        days_dep = (depart - date.fromtimestamp(obs)).days
        search_id = f"{k:08x}"
        for j in range(OFFERS_PER_SEARCH):
            rows.append((
                f"o{k}_{j}", SITES[j % len(SITES)], origin, destination, depart.isoformat(), None, 1,
                rng.randint(3000, 30000), "RUB", obs, days_dep, None, "SU", None, None, search_id,
            ))
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO price_history (offer_id, site, origin, destination, depart, return_date, adults, "
            "price, currency, observed_at, days_to_departure, baggage, airline, flight_numbers, depart_at, "
            "search_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("loop_responsive")
    rng = random.Random(7)
    now = time.time()
    _fill_metrics(d / "metrics.sqlite", now, rng)
    _fill_history(d / "history.sqlite", now, rng)
    return d


@pytest.fixture(autouse=True)
def _clean_state():
    reset_fuse_state()
    reset_speed_tables()
    yield
    reset_fuse_state()
    reset_speed_tables()


# ============================================================================
# Замер: на сколько опаздывает таймер в 10 мс
# ============================================================================

class StallMeter:
    """Корутина-таймер спит по TICK и запоминает наибольшее опоздание пробуждения."""

    def __init__(self) -> None:
        self.max_stall = 0.0
        self.ticks = 0
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(TICK)
            self.max_stall = max(self.max_stall, time.perf_counter() - t0 - TICK)
            self.ticks += 1

    async def __aenter__(self) -> StallMeter:
        self._task = asyncio.create_task(self._run())
        # таймер уже спит, когда пойдёт нагрузка
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc) -> None:
        # последний тик после нагрузки тоже должен попасть в замер
        await asyncio.sleep(TICK * 3)
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


async def _assert_loop_responsive(run_once, what: str) -> None:
    """Нагрузка не держит цикл дольше MAX_STALL хотя бы в одном из ATTEMPTS прогонов.

    Код, который стоит в цикле событий, стопорит его в каждом прогоне (без правки — от 0.3 до 2 с
    каждый раз). Машина под нагрузкой (очередь симулятора) даёт редкие случайные всплески опоздания
    таймера, поэтому неудачный прогон повторяется, а в сообщении — все замеры.
    """
    stalls: list[float] = []
    for attempt in range(ATTEMPTS):
        async with StallMeter() as meter:
            await run_once(attempt)
        assert meter.ticks > 0, f"{what}: таймер ни разу не проснулся"
        stalls.append(meter.max_stall)
        if meter.max_stall < MAX_STALL:
            print(f"{what}: опоздание таймера {', '.join(f'{x * 1000:.0f}' for x in stalls)} мс")
            return
    shown = ", ".join(f"{x * 1000:.0f}" for x in stalls)
    pytest.fail(f"{what}: цикл событий стоял {shown} мс за {len(stalls)} прогона (порог {MAX_STALL * 1000:.0f} мс)")


# ============================================================================
# 1. Живой поиск: старт сайта через предохранитель
# ============================================================================

@pytest.mark.asyncio
async def test_live_searches_site_start_does_not_stall_loop(data_dir, monkeypatch):
    """20 живых поисков разом по 5 сайтам: предохранитель и раскладка не стопорят цикл."""
    rng = random.Random(11)

    async def fake_fetch(settings, cands, url, **kwargs):
        # телефоны отвечают в разное время, как в жизни
        await asyncio.sleep(rng.uniform(0.05, 0.4))
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = list(cands)[0]
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='[{"price": 5000}]'), True

    def parser(snap, q):
        return [Offer(site="loop", price=5000, airline="SU")]

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)
    monkeypatch.setitem(PARSERS, PARSER_NAME, parser)

    sites = [
        SiteConfig(name, "https://loop.test/{origin}/{destination}/{date_iso}", phone="dev1,dev2", parser=PARSER_NAME)
        for name in SITES
    ]
    # metrics=None, как в проде: бот не кладёт MetricsStore в settings
    settings = Settings(
        sites=sites,
        storage_dir=data_dir,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        price_timeline=False,
    )
    queries = [Query("MOW", "AER", date.today() + timedelta(days=10 + i)) for i in range(CALLS)]

    async def run_once(attempt: int) -> None:
        # каждый прогон — с холодной таблицей скоростей и пустым предохранителем, как первый
        reset_speed_tables()
        reset_fuse_state()
        guard = SearchGuard(min_interval=0.0, retry_delay=0.0)
        results = await asyncio.gather(*(
            search(q, settings, priority=0, guard=guard, search_id=f"looplive{attempt}_{i}")
            for i, q in enumerate(queries)
        ))
        assert len(results) == CALLS
        for res in results:
            assert len(res) == len(SITES)
            assert all(r.ok for r in res), [(r.site, r.error) for r in res if not r.ok]
        # разборы страниц не ждали в очереди потоков дольше таймаута: каждая попытка — с первого раза
        with sqlite3.connect(data_dir / "metrics.sqlite") as db:
            outcomes = dict(db.execute(
                "SELECT outcome, COUNT(*) FROM attempts WHERE search_id LIKE ? GROUP BY outcome",
                (f"looplive{attempt}_%",),
            ).fetchall())
        assert outcomes == {"ok": CALLS * len(SITES)}

    await _assert_loop_responsive(run_once, "живой поиск")


# ============================================================================
# 2. Выбор кандидатов фонового сбора
# ============================================================================

@pytest.mark.asyncio
async def test_bg_candidates_do_not_stall_loop(data_dir):
    """20 шагов фонового сбора разом: выбор кандидатов по истории не стопорит цикл."""
    history = PriceHistory(data_dir / "history.sqlite")
    settings = Settings(sites=[], storage_dir=data_dir, bg_collect=True, bg_collect_max_per_day=10_000)

    async def search_fn(q):
        await asyncio.sleep(0.01)
        return []

    collector = BackgroundCollector(history=history, search_fn=search_fn, is_idle=lambda: True, settings=settings)

    async def run_once(attempt: int) -> None:
        done = await asyncio.gather(*(collector.run_once() for _ in range(CALLS)))
        assert all(done)

    await _assert_loop_responsive(run_once, "кандидаты фона")


# ============================================================================
# 3. /stats и история цены
# ============================================================================

class _QuietBot(Bot):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.sent: list[tuple[str, dict]] = []

    async def call(self, method: str, **params):
        self.sent.append((method, params))
        return {"message_id": len(self.sent), "chat": {"id": params.get("chat_id")}}


def _make_bot(data_dir) -> _QuietBot:
    settings = Settings(sites=[], tg_token="test-token", tg_allowed_ids={1}, storage_dir=data_dir)
    bot = _QuietBot(settings)
    # conftest подменяет пути баз на tmp_path теста; тяжёлые базы — общие для модуля
    bot.history = PriceHistory(data_dir / "history.sqlite")
    bot.metrics = MetricsStore(data_dir / "metrics.sqlite")
    return bot


@pytest.mark.asyncio
async def test_stats_command_does_not_stall_loop(data_dir):
    """20 команд /stats разом: сводка по истории не стопорит цикл."""
    bot = _make_bot(data_dir)

    async def run_once(attempt: int) -> None:
        bot.sent.clear()
        await asyncio.gather(*(bot._admin_command(1, "/stats") for _ in range(CALLS)))
        texts = [p.get("text", "") for m, p in bot.sent if m == "sendMessage"]
        assert len(texts) == CALLS
        assert all("Популярные маршруты (7 дней)" in t for t in texts)
        assert all("MOW" in t or "Москва" in t for t in texts)

    await _assert_loop_responsive(run_once, "/stats")


@pytest.mark.asyncio
async def test_history_request_price_curve_does_not_stall_loop(data_dir):
    """20 запросов истории цены разом: price_curve по всей истории не стопорит цикл."""
    bot = _make_bot(data_dir)
    req = HistoryRequest(query=Query("MOW", "AER", date.today() + timedelta(days=8)))

    async def run_once(attempt: int) -> None:
        bot.sent.clear()
        await asyncio.gather(*(bot._handle_history_request(1, req) for _ in range(CALLS)))
        assert len(bot.sent) == CALLS

    await _assert_loop_responsive(run_once, "история цены")


# ============================================================================
# 4. Перенос не меняет решений
# ============================================================================

def test_metrics_schema_checked_once_per_file(tmp_path, monkeypatch):
    """Новый MetricsStore на ту же базу не гоняет схему заново; заменённый файл — проверяется снова."""
    path = tmp_path / "metrics.sqlite"
    MetricsStore(path).record_run(
        source="user", site="s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0
    )
    applied = []
    real_apply = MetricsStore._apply_schema

    def counting_apply(self):
        applied.append(self.path)
        real_apply(self)

    monkeypatch.setattr(MetricsStore, "_apply_schema", counting_apply)

    # как предохранитель и хеджи: новый MetricsStore на каждый вызов
    for _ in range(5):
        assert MetricsStore(path).site_recent_outcomes("s1", 0)
    assert applied == []

    # файл подменили (например, вернули из копии) базой без наших таблиц — схема ставится снова
    other = tmp_path / "other.sqlite"
    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE unrelated (x INTEGER)")
    os.replace(other, path)
    store = MetricsStore(path)
    store.record_run(
        source="user", site="s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0
    )
    assert len(applied) == 1
    assert store.site_recent_outcomes("s1", 0)


@pytest.mark.asyncio
async def test_fuse_async_lets_exactly_one_probe(tmp_path):
    """20 стартов деградировавшего сайта разом, когда пора пробовать: проба одна, как в цикле событий."""
    from avia.dispatcher import check_site_fuse_async

    store = MetricsStore(tmp_path / "metrics.sqlite")
    settings = Settings(
        sites=[],
        storage_dir=tmp_path,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
        site_fuse_probe_seconds=600,
    )
    t0 = time.time() - 3600
    for dev, ts in (("dev1", t0), ("dev2", t0 + 1)):
        store.record_run(
            source="user", site="s_fused", ok=False, partial=False, captcha=False, error="502 Bad Gateway",
            offers=0, elapsed=2.0, device=dev, ts=ts,
        )
    assert check_site_fuse("s_fused", settings, now=t0 + 10) == (True, False)

    probe_now = t0 + 1 + 601
    res = await asyncio.gather(*(
        check_site_fuse_async("s_fused", settings, now=probe_now, consume_probe=True) for _ in range(CALLS)
    ))
    assert res.count((False, False)) == 1
    assert res.count((True, False)) == CALLS - 1


_BUCKETS = (("0-7", 0, 7), ("8-14", 8, 14), ("15-21", 15, 21), ("22-30", 22, 30), ("31-60", 31, 60), ("61+", 61, 10**9))


def _sample_key(search_id, observed_at) -> str:
    return str(search_id) if search_id else f"m_{round(observed_at / 60.0)}"


def _popular_routes_by_rows(rows, limit: int) -> list[tuple[str, str, int]]:
    """Прежний подсчёт popular_routes по каждой строке окна: число разных замеров на маршрут."""
    samples: dict[tuple[str, str], set[str]] = {}
    for origin, destination, search_id, observed_at in rows:
        samples.setdefault((origin, destination), set()).add(_sample_key(search_id, observed_at))
    res = [(o, d, len(keys)) for (o, d), keys in samples.items()]
    res.sort(key=lambda x: (-x[2], x[0], x[1]))
    return res[:limit]


def _price_curve_by_rows(rows, min_samples: int) -> dict[str, dict]:
    """Прежний расчёт price_curve по каждой строке окна (строки по observed_at)."""
    samples: dict[tuple, list] = {}
    for origin, destination, depart, ret, adults, price, search_id, observed_at, days_dep in rows:
        key = ((origin, destination, depart, ret, adults), _sample_key(search_id, observed_at))
        cur = samples.get(key)
        if cur is None:
            if days_dep is not None:
                d_val = int(days_dep)
            else:
                d_val = (date.fromisoformat(depart) - datetime.fromtimestamp(observed_at).date()).days
            samples[key] = [int(price), d_val]
        else:
            cur[0] = min(cur[0], int(price))
    by_route: dict[tuple, list] = {}
    for (route, _), item in samples.items():
        by_route.setdefault(route, []).append(item)
    ratios: dict[str, list[float]] = {name: [] for name, _, _ in _BUCKETS}
    for items in by_route.values():
        median = float(statistics.median(best for best, _ in items))
        for best, d_val in items:
            if d_val < 0:
                continue
            name = next(n for n, lo, hi in _BUCKETS if lo <= d_val <= hi)
            ratios[name].append(best / median)
    return {
        name: {"ratio": round(float(statistics.median(r)), 4), "samples": len(r)}
        for name, r in ratios.items()
        if len(r) >= max(1, min_samples)
    }


def test_sql_rollup_keeps_popular_routes_and_price_curve(tmp_path):
    """Свёртка в SQLite (замер — одна строка) даёт тот же ответ, что и подсчёт по каждой строке."""
    rng = random.Random(5)
    now = time.time()
    path = tmp_path / "history.sqlite"
    history = PriceHistory(path)
    today = date.fromtimestamp(now)
    rows = []
    for k in range(400):
        obs = now - rng.uniform(0, 40 * 86400)
        origin, destination = ROUTES[k % 6]
        depart = today + timedelta(days=rng.randint(-5, 80))
        days_dep = (depart - date.fromtimestamp(obs)).days
        ret = rng.choice([None, "", (depart + timedelta(days=7)).isoformat()])
        # старые строки без search_id и без days_to_departure — тоже в выборке
        search_id = rng.choice([f"sid{k}", f"sid{k}", None, ""])
        stored_days = days_dep if rng.random() > 0.2 else None
        for j in range(rng.randint(1, 6)):
            rows.append((
                f"o{k}_{j}", "s1", origin, destination, depart.isoformat(), ret, 1, rng.randint(1000, 9000),
                "RUB", obs, stored_days, None, "SU", None, None, search_id,
            ))
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO price_history (offer_id, site, origin, destination, depart, return_date, adults, "
            "price, currency, observed_at, days_to_departure, baggage, airline, flight_numbers, depart_at, "
            "search_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    with sqlite3.connect(path) as db:
        for days in (7, 30):
            raw = db.execute(
                "SELECT origin, destination, search_id, observed_at FROM price_history "
                "WHERE observed_at >= ? AND observed_at <= ?",
                (now - days * 86400.0, now),
            ).fetchall()
            assert history.popular_routes(limit=10, days=days, now=now) == _popular_routes_by_rows(raw, 10)
        raw_curve = db.execute(
            "SELECT origin, destination, depart, COALESCE(return_date, ''), adults, price, search_id, "
            "observed_at, days_to_departure FROM price_history WHERE observed_at >= ? AND observed_at <= ? "
            "ORDER BY observed_at ASC",
            (now - 180 * 86400.0, now),
        ).fetchall()
    expected = _price_curve_by_rows(raw_curve, min_samples=1)
    assert expected
    assert history.price_curve(now=now, min_samples=1) == expected
