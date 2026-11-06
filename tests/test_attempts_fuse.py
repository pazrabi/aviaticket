"""Тесты для шагов 5/11/12 плана PLAN_SPEED.md:

1. Таймаут разбора страниц парсером в потоке (asyncio.to_thread).
2. Метрики попыток (таблица attempts, record_attempt, роль primary/retry, search_id).
3. Предохранитель сайтов (site fuse): деградация сайтов в живом поиске при сбое на 2+ телефонах.
"""
from __future__ import annotations

import asyncio
from datetime import date
import sqlite3
import time

import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.dispatcher import check_site_fuse, plan_live_assignment, reset_fuse_state
from avia.formatting import is_unavailable
from avia.guard import SearchGuard
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.parsers import PARSERS
from avia.pipeline import _process_snapshot, guard as default_guard, search


@pytest.fixture(autouse=True)
def clean_state():
    """Сбрасывает состояние SearchGuard и предохранителя сайтов перед и после каждого теста."""
    reset_fuse_state()
    default_guard.clear()
    orig_interval = default_guard.min_interval
    orig_retry = default_guard.retry_delay
    default_guard.min_interval = 0.0
    default_guard.retry_delay = 0.0
    yield
    reset_fuse_state()
    default_guard.clear()
    default_guard.min_interval = orig_interval
    default_guard.retry_delay = orig_retry


# ============================================================================
# 1. ТАЙМАУТ РАЗБОРА (Parser Timeout)
# ============================================================================

@pytest.mark.asyncio
async def test_parser_timeout_hanging_parser_does_not_block_loop(tmp_path, monkeypatch):
    """Зависший парсер (time.sleep 0.5 при таймауте 0.05) не блокирует событийный цикл и даёт ошибку."""
    loop_ticks = 0

    async def background_counter():
        nonlocal loop_ticks
        while True:
            await asyncio.sleep(0.01)
            loop_ticks += 1

    bg_task = asyncio.create_task(background_counter())

    def hanging_parser(snap, q):
        time.sleep(0.5)
        return [Offer(site="slow_site", price=5000, airline="SU")]

    monkeypatch.setitem(PARSERS, "hanging_parser", hanging_parser)

    site = SiteConfig("slow_site", "https://site.test", parser="hanging_parser")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        parser_timeout_seconds=0.05,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))
    snap = Snapshot(url="https://site.test", text="some html")

    # Проверяем, что _process_snapshot выбрасывает TimeoutError с ожидаемым текстом
    with pytest.raises(TimeoutError, match="разбор не уложился в таймаут"):
        await _process_snapshot(site, snap, q, settings)

    # Проверяем, что событийный цикл не был заблокирован (фоновая задача тикала)
    assert loop_ticks >= 2

    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_parser_timeout_normal_and_disabled(tmp_path, monkeypatch):
    """Нормальный парсер отрабатывает штатно, а при parser_timeout_seconds=0 таймаут отключён."""
    def normal_parser(snap, q):
        return [Offer(site="fast_site", price=3000, airline="S7")]

    monkeypatch.setitem(PARSERS, "fast_parser", normal_parser)

    site = SiteConfig("fast_site", "https://site.test", parser="fast_parser")
    q = Query("MOW", "AER", date(2026, 12, 10))
    snap = Snapshot(url="https://site.test", text="some html")

    # Обычный вызов с таймаутом 5.0
    settings = Settings(sites=[site], storage_dir=tmp_path, parser_timeout_seconds=5.0)
    offers, url, partial = await _process_snapshot(site, snap, q, settings)
    assert len(offers) == 1
    assert offers[0].price == 3000

    # Вызов с отключённым таймаутом (0)
    settings_zero = Settings(sites=[site], storage_dir=tmp_path, parser_timeout_seconds=0)
    offers, url, partial = await _process_snapshot(site, snap, q, settings_zero)
    assert len(offers) == 1
    assert offers[0].price == 3000


# ============================================================================
# 2. МЕТРИКИ ПОПЫТОК (Attempts Metrics)
# ============================================================================

def test_attempts_table_schema(tmp_path):
    """Таблица attempts создаётся идемпотентно в _ensure_schema со всеми нужными полями."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    store._ensure_schema()

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(attempts)").fetchall()}
        expected = {
            "id", "ts", "search_id", "site", "device", "source",
            "role", "outcome", "offers", "fetch_seconds", "queue_wait",
        }
        assert expected.issubset(cols), f"Не хватает колонок: {expected - cols}"

    # Идемпотентность повторного вызова
    store._ensure_schema()


@pytest.mark.asyncio
async def test_attempts_recorded_on_success(tmp_path, monkeypatch):
    """Попытка успешно записывается с ролью primary, outcome='ok' и search_id."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site.test", phone="dev1", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
    )
    q = Query("MOW", "LED", date(2026, 12, 10))

    def p1(snap, q):
        return [Offer(site="site1", price=4500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    async def fake_fetch(settings, cands, url, **kwargs):
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev1"
            stats["fetch_seconds"] = 1.2
            stats["queue_wait"] = 0.3
        return Snapshot(url=url, text='{"ok": true}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    custom_search_id = "abcd1234"
    results = await search(q, settings, search_id=custom_search_id)
    assert len(results) == 1
    assert results[0].ok is True

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attempts WHERE site = 'site1'").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["search_id"] == custom_search_id
        assert r["site"] == "site1"
        assert r["device"] == "dev1"
        assert r["source"] == "user"
        assert r["role"] == "primary"
        assert r["outcome"] == "ok"
        assert r["offers"] == 1
        assert abs(r["fetch_seconds"] - 1.2) < 0.01
        assert abs(r["queue_wait"] - 0.3) < 0.01


@pytest.mark.asyncio
async def test_attempts_recorded_on_captcha_and_retry(tmp_path, monkeypatch):
    """При капче на первом телефоне пишется primary/captcha, на втором телефоне — retry/ok."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_cap", "https://site.test", phone="dev1,dev2", parser="p_cap")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        speed_routing=False,
    )
    q = Query("MOW", "LED", date(2026, 12, 10))

    def p_cap(snap, q):
        return [Offer(site="site_cap", price=4000, airline="DP")]

    monkeypatch.setitem(PARSERS, "p_cap", p_cap)

    call_count = 0

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal call_count
        call_count += 1
        stats = kwargs.get("stats")
        dev = list(cands)[0]
        if stats is not None:
            stats["device"] = dev
            stats["fetch_seconds"] = 1.0
            stats["queue_wait"] = 0.1
        if dev == "dev1":
            # Первый телефон выдаёт капчу (blocked=True)
            return Snapshot(url=url, text="Blocked by captcha", blocked=True), True
        return Snapshot(url=url, text="OK results", blocked=False), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    results = await search(q, settings)
    assert len(results) == 1
    assert results[0].ok is True

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attempts WHERE site = 'site_cap' ORDER BY id ASC").fetchall()
        assert len(rows) == 2

        # 1-я попытка на dev1: капча
        r1 = rows[0]
        assert r1["device"] == "dev1"
        assert r1["role"] == "primary"
        assert r1["outcome"] == "captcha"
        assert r1["offers"] == 0

        # 2-я попытка на dev2: повтор (retry)
        r2 = rows[1]
        assert r2["device"] == "dev2"
        assert r2["role"] == "retry"
        assert r2["outcome"] == "ok"
        assert r2["offers"] == 1
        # search_id общий для обеих попыток
        assert r1["search_id"] == r2["search_id"]


# ============================================================================
# 3. САЙТ «ДЕГРАДИРОВАЛ» В ЖИВОМ ПОИСКЕ (Site Fuse)
# ============================================================================

@pytest.mark.asyncio
async def test_fuse_two_different_phones_failed_skips_live_search(tmp_path, monkeypatch):
    """(а) 2 разных телефона провалились в окне и нет успехов → сайт пропускается без вызова телефонов."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_degraded", "https://site.test", phone="dev1,dev2", parser="p_fuse")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    # Записываем сбои на 2 разных телефонах за последние 60 секунд (один из сбоев с капчей)
    now = time.time()
    store.record_run(source="user", site="site_degraded", ok=False, partial=False, captcha=True,
                     error="капча", offers=0, elapsed=2.0, device="dev1", ts=now - 60)
    store.record_run(source="bg", site="site_degraded", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev2", ts=now - 30)

    # Проверяем раскладку dispatcher: сайт исключён из eligible_sites
    assignment = plan_live_assignment(settings, [site], now=now)
    assert "site_degraded" not in assignment or assignment["site_degraded"] == []

    # Флаг вызова телефонов
    phones_called = False

    async def fake_fetch(*args, **kwargs):
        nonlocal phones_called
        phones_called = True
        return Snapshot(url="https://test", text=""), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    # Живой поиск (priority=0) пропускает сайт без вызова телефонов
    results = await search(q, settings, priority=0, now=now)
    assert len(results) == 1
    res = results[0]
    assert phones_called is False
    assert res.ok is False
    assert res.error == "сайт временно недоступен"
    assert res.captcha is True
    assert res.priority == 0
    assert res.elapsed == 0.0
    # formatting.is_unavailable распознаёт как недоступный
    assert is_unavailable(res) is True


@pytest.mark.asyncio
async def test_fuse_single_phone_failed_twice_does_not_trip(tmp_path, monkeypatch):
    """(б) один телефон провалился дважды → не срабатывает (нужно >= 2 РАЗНЫХ телефона)."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_one_dev", "https://site.test", phone="dev1,dev2", parser="p_ok")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    def p_ok(snap, q):
        return [Offer(site="site_one_dev", price=3500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p_ok", p_ok)

    now = time.time()
    # Два сбоя на одном и том же dev1
    store.record_run(source="user", site="site_one_dev", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev1", ts=now - 50)
    store.record_run(source="user", site="site_one_dev", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev1", ts=now - 20)

    phones_called = False

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal phones_called
        phones_called = True
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev2"
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='{"price": 3500}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    results = await search(q, settings, priority=0, now=now)
    assert len(results) == 1
    assert phones_called is True
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_fuse_success_in_window_does_not_trip(tmp_path, monkeypatch):
    """(в) есть успех в окне → не срабатывает."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_with_success", "https://site.test", phone="dev1,dev2", parser="p_ok")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    def p_ok(snap, q):
        return [Offer(site="site_with_success", price=3500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p_ok", p_ok)

    now = time.time()
    # Сбои на 2 телефонах, но затем успех на dev3
    store.record_run(source="user", site="site_with_success", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev1", ts=now - 80)
    store.record_run(source="user", site="site_with_success", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev2", ts=now - 40)
    store.record_run(source="bg", site="site_with_success", ok=True, partial=False, captcha=False,
                     error=None, offers=5, elapsed=1.5, device="dev3", ts=now - 10)

    phones_called = False

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal phones_called
        phones_called = True
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev3"
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='{"price": 3500}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    results = await search(q, settings, priority=0, now=now)
    assert len(results) == 1
    assert phones_called is True
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_fuse_probe_attempt_after_probe_seconds(tmp_path, monkeypatch):
    """(г) пробная попытка раз в site_fuse_probe_seconds (подмени время); удачная попытка снимает режим."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_probe", "https://site.test", phone="dev1,dev2", parser="p_probe")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
        site_fuse_probe_seconds=600,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    def p_probe(snap, q):
        return [Offer(site="site_probe", price=3200, airline="SU")]

    monkeypatch.setitem(PARSERS, "p_probe", p_probe)

    t0 = 1000.0
    store.record_run(source="user", site="site_probe", ok=False, partial=False, captcha=False,
                     error="err1", offers=0, elapsed=2.0, device="dev1", ts=t0 - 10)
    store.record_run(source="user", site="site_probe", ok=False, partial=False, captcha=False,
                     error="err2", offers=0, elapsed=2.0, device="dev2", ts=t0)

    phones_called = 0

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal phones_called
        phones_called += 1
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev1"
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='{"price": 3200}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    # 1. Поиск через 10 секунд (t0 + 10): предохранитель активен, сайт пропускается
    res1 = await search(q, settings, priority=0, now=t0 + 10)
    assert res1[0].ok is False
    assert res1[0].error == "сайт временно недоступен"
    assert phones_called == 0

    # 2. Поиск через 601 секунду (t0 + 601 >= site_fuse_probe_seconds): выполняется пробная попытка
    res2 = await search(q, settings, priority=0, now=t0 + 601)
    assert phones_called == 1
    assert res2[0].ok is True

    # Фиксируем успешный запуск в базе (как делает бот через record_results)
    store.record_run(source="user", site="site_probe", ok=True, partial=False, captcha=False,
                     error=None, offers=1, elapsed=1.0, device="dev1", ts=t0 + 601)

    # 3. Следующий поиск (t0 + 605): удачная попытка сняла режим деградации, сайт работает штатно
    default_guard.clear()
    res3 = await search(q, settings, priority=0, now=t0 + 605)
    assert phones_called == 2
    assert res3[0].ok is True


@pytest.mark.asyncio
async def test_fuse_disabled_by_settings_or_env(tmp_path, monkeypatch):
    """(д) SITE_FUSE=0 / site_fuse_enabled=False отключает предохранитель."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_disabled_fuse", "https://site.test", phone="dev1,dev2", parser="p_ok")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=False,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    def p_ok(snap, q):
        return [Offer(site="site_disabled_fuse", price=3000, airline="SU")]

    monkeypatch.setitem(PARSERS, "p_ok", p_ok)

    now = time.time()
    store.record_run(source="user", site="site_disabled_fuse", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev1", ts=now - 20)
    store.record_run(source="user", site="site_disabled_fuse", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev2", ts=now - 10)

    phones_called = False

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal phones_called
        phones_called = True
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev1"
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='{"price": 3000}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    results = await search(q, settings, priority=0, now=now)
    assert len(results) == 1
    assert phones_called is True
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_fuse_priority_greater_than_zero_unaffected(tmp_path, monkeypatch):
    """(е) priority > 0 не затронут (подписки и фон опрашивают сайт как обычно)."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_prio", "https://site.test", phone="dev1,dev2", parser="p_ok")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    def p_ok(snap, q):
        return [Offer(site="site_prio", price=3000, airline="SU")]

    monkeypatch.setitem(PARSERS, "p_ok", p_ok)

    now = time.time()
    store.record_run(source="user", site="site_prio", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev1", ts=now - 20)
    store.record_run(source="user", site="site_prio", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev2", ts=now - 10)

    phones_called = False

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal phones_called
        phones_called = True
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev1"
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='{"price": 3000}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    # При priority=1 (подписки) предохранитель не срабатывает
    results = await search(q, settings, priority=1, now=now)
    assert len(results) == 1
    assert phones_called is True
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_fuse_probe_success_empty_offers_clears_degradation(tmp_path, monkeypatch):
    """(ж) успешная пробная попытка без офферов (offers == []) снимает режим деградации."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_empty_ok", "https://site.test", phone="dev1,dev2", parser="p_empty")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
        site_fuse_probe_seconds=600,
    )
    q = Query("MOW", "AER", date(2026, 12, 10))

    # Парсер возвращает пустой список офферов (на эту дату билетов нет, но страница открылась штатно)
    def p_empty(snap, q):
        return []

    monkeypatch.setitem(PARSERS, "p_empty", p_empty)

    t0 = 2000.0
    store.record_run(source="user", site="site_empty_ok", ok=False, partial=False, captcha=False,
                     error="err1", offers=0, elapsed=2.0, device="dev1", ts=t0 - 10)
    store.record_run(source="user", site="site_empty_ok", ok=False, partial=False, captcha=False,
                     error="err2", offers=0, elapsed=2.0, device="dev2", ts=t0)

    phones_called = 0

    async def fake_fetch(settings, cands, url, **kwargs):
        nonlocal phones_called
        phones_called += 1
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = "dev1"
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='{"offers": []}'), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)

    # 1. Поиск через 10 секунд (t0 + 10): предохранитель активен, сайт пропускается
    res0 = await search(q, settings, priority=0, now=t0 + 10)
    assert res0[0].ok is False
    assert res0[0].error == "сайт временно недоступен"
    assert phones_called == 0

    # 2. Пробная попытка через 601 секунду
    res1 = await search(q, settings, priority=0, now=t0 + 601)
    assert phones_called == 1
    assert res1[0].ok is True
    assert res1[0].offers == []

    # Режим деградации снят: _FUSE_STATE["site_empty_ok"]["degraded"] == False
    from avia.dispatcher import _FUSE_STATE
    assert _FUSE_STATE["site_empty_ok"]["degraded"] is False

    # 3. Следующий поиск работает сразу, не отсекая сайт
    default_guard.clear()
    res2 = await search(q, settings, priority=0, now=t0 + 605)
    assert phones_called == 2
    assert res2[0].ok is True


def test_get_metrics_caches_store_on_settings(tmp_path):
    """_get_metrics кэширует созданный экземпляр MetricsStore на объекте Settings."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    # Инициализируем файл базы данных
    store.record_run(source="user", site="test", ok=True, partial=False, captcha=False,
                     error=None, offers=0, elapsed=1.0, device="d1")

    settings = Settings(
        sites=[],
        storage_dir=tmp_path,
        metrics=None,
    )
    from avia.dispatcher import _get_metrics

    m1 = _get_metrics(settings)
    assert m1 is not None
    assert settings.metrics is m1

    m2 = _get_metrics(settings)
    assert m2 is m1


def test_fuse_captcha_updated_on_probe_failure_and_horizon_bounded(tmp_path):
    """Повторные сбои при деградации обновляют had_captcha и ограничивают выборку окном window_seconds."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site_long_deg", "https://site.test", phone="dev1,dev2", parser="p_ok")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        site_fuse_enabled=True,
        site_fuse_window_seconds=300,
        site_fuse_min_phones=2,
        site_fuse_probe_seconds=600,
    )

    t0 = 10000.0
    # Исходные сбои без капчи (502)
    store.record_run(source="user", site="site_long_deg", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev1", ts=t0 - 50)
    store.record_run(source="user", site="site_long_deg", ok=False, partial=False, captcha=False,
                     error="502", offers=0, elapsed=2.0, device="dev2", ts=t0 - 20)

    fused, had_captcha = check_site_fuse("site_long_deg", settings, now=t0, consume_probe=False)
    assert fused is True
    assert had_captcha is False

    # Спустя долгое время (через 3 дня) проба падает с капчей
    t_probe = t0 + 3 * 86400
    fused_probe, _ = check_site_fuse("site_long_deg", settings, now=t_probe, consume_probe=True)
    assert fused_probe is False  # Разрешена проба

    # Проба записана как капча
    store.record_run(source="user", site="site_long_deg", ok=False, partial=False, captcha=True,
                     error="капча", offers=0, elapsed=2.0, device="dev1", ts=t_probe)

    # Запоминаем запросы в site_recent_outcomes для проверки горизонта
    queried_since: list[float] = []
    orig_recent = store.site_recent_outcomes

    def wrapped_recent(s, since_ts):
        queried_since.append(since_ts)
        return orig_recent(s, since_ts)

    store.site_recent_outcomes = wrapped_recent

    # Следующая проверка через 10 секунд после неудачной пробы
    fused_after, had_cap_after = check_site_fuse("site_long_deg", settings, now=t_probe + 10, consume_probe=False)
    assert fused_after is True
    assert had_cap_after is True  # Капча обновилась
    assert len(queried_since) >= 1
    # since_ts ограничен окном window_seconds (300 с), а не тянется с первого сбоя t0
    assert queried_since[-1] >= t_probe + 10 - 300


def test_is_unavailable_recognizes_temporarily_unavailable():
    """formatting.is_unavailable распознаёт ошибку 'сайт временно недоступен'."""
    res = SiteResult(
        site="test_site",
        ok=False,
        offers=[],
        error="сайт временно недоступен",
        captcha=False,
    )
    assert is_unavailable(res) is True
