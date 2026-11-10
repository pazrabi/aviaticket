"""Тесты таймаута разбора, метрик попыток и предохранителя сайтов:

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
from avia.metrics import MetricsStore, wait_metrics_io
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

    assert wait_metrics_io()  # попытки пишутся без ожидания: дождаться записи
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

    assert wait_metrics_io()  # попытки пишутся без ожидания: дождаться записи
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
                     error="телефон недоступен (502)", offers=0, elapsed=2.0, device="dev1", ts=t0 - 10)
    store.record_run(source="user", site="site_probe", ok=False, partial=False, captcha=False,
                     error="телефон недоступен (502)", offers=0, elapsed=2.0, device="dev2", ts=t0)

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


def test_empty_results_do_not_trigger_fuse(tmp_path):
    """Пустая выдача (редкий маршрут без билетов) не считается поломкой сайта."""
    from avia.dispatcher import check_site_fuse, reset_fuse_state

    reset_fuse_state()
    store = MetricsStore(tmp_path / "metrics.sqlite")
    t0 = 5000.0
    for i, dev in enumerate(["a", "b", "c"]):
        store.record_run(source="user", site="s_empty", ok=False, partial=False, captcha=False,
                         error="формат выдачи мог измениться, 0 вариантов при непустой странице",
                         offers=0, elapsed=3.0, device=dev, ts=t0 + i)
    settings = Settings(sites=[], storage_dir=tmp_path, metrics=store)
    assert check_site_fuse("s_empty", settings, now=t0 + 10)[0] is False
    reset_fuse_state()
    # а вот капчи на двух телефонах срабатывают
    for i, dev in enumerate(["a", "b"]):
        store.record_run(source="user", site="s_cap", ok=False, partial=False, captcha=True,
                         error="сайт показал капчу на телефоне", offers=0, elapsed=3.0, device=dev, ts=t0 + i)
    assert check_site_fuse("s_cap", settings, now=t0 + 10)[0] is True
    reset_fuse_state()
