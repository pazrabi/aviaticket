"""Тесты для хеджей живого поиска.

Требования:
- хедж выигрывает быстрый, проигравший отменён, телефон освобождён;
- хедж не стартует без свободного телефона;
- не стартует для нерискованной пары;
- не больше 1 на сайт и 2 на поиск;
- HEDGE=0 — старое поведение;
- ошибка основной копии → результат от хеджа.
"""
from __future__ import annotations

import asyncio
from datetime import date
import sqlite3
import time
from typing import Any

import httpx
import pytest

from avia.config import Settings, SiteConfig
from avia.dispatcher import reset_fuse_state
from avia.droidhub import (
    _busy_devices,
    _reset_pool,
    acquire,
    try_acquire_free,
)
from avia.hedge import HedgeTracker, hedge_risky
from avia.metrics import MetricsStore, wait_metrics_io
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import guard as default_guard, search, _run_site


_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _mock_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "avia.droidhub.httpx.AsyncClient",
        lambda **kwargs: _ORIG_ASYNC_CLIENT(
            transport=transport,
            **{k: v for k, v in kwargs.items() if k != "transport"},
        ),
    )


@pytest.fixture(autouse=True)
def clean_env():
    """Сбрасывает состояние пула устройств, предохранителя и guard перед каждым тестом."""
    _reset_pool()
    reset_fuse_state()
    default_guard.clear()
    orig_interval = default_guard.min_interval
    orig_retry = default_guard.retry_delay
    default_guard.min_interval = 0.0
    default_guard.retry_delay = 0.0
    yield
    _reset_pool()
    reset_fuse_state()
    default_guard.clear()
    default_guard.min_interval = orig_interval
    default_guard.retry_delay = orig_retry


# ============================================================================
# 1. ТЕСТЫ ФУНКЦИИ hedge_risky
# ============================================================================


def test_hedge_risky_unit(tmp_path):
    """Проверка логики hedge_risky:

    - < 5 попыток → False
    - >= 5 попыток и fail_share >= 0.15 → True
    - >= 5 попыток и fail_share < 0.15 → False
    - p90 > LIVE_SAFETY_SECONDS/2 (45.0 с) → True
    - ошибка чтения / нет базы → False
    - settings.hedge=False → False
    """
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    settings = Settings(sites=[], storage_dir=tmp_path, metrics=store, live_safety_seconds=90.0)

    now = 1_000_000.0

    # 1. Меньше 5 попыток (4 попытки, 1 сбой = 25%) → False (< 5 попыток)
    store.record_attempt(site="s1", device="dev1", outcome="ok", offers=2, fetch_seconds=5.0, ts=now - 100)
    store.record_attempt(site="s1", device="dev1", outcome="ok", offers=2, fetch_seconds=5.0, ts=now - 90)
    store.record_attempt(site="s1", device="dev1", outcome="ok", offers=2, fetch_seconds=5.0, ts=now - 80)
    store.record_attempt(site="s1", device="dev1", outcome="error", offers=0, fetch_seconds=5.0, ts=now - 70)
    assert hedge_risky("s1", "dev1", settings, now=now) is False

    # 2. Добавляем 5-ю попытку (успех) → 1/5 = 20% >= 15% → True
    store.record_attempt(site="s1", device="dev1", outcome="ok", offers=2, fetch_seconds=5.0, ts=now - 60)
    assert hedge_risky("s1", "dev1", settings, now=now) is True

    # 3. Добавляем еще успехов, чтобы fail_share упал ниже 15% (1/10 = 10%) → False
    for i in range(5):
        store.record_attempt(site="s1", device="dev1", outcome="ok", offers=2, fetch_seconds=5.0, ts=now - 50 + i)
    assert hedge_risky("s1", "dev1", settings, now=now) is False

    # 4. Медленная пара: fail_share = 0, но p90 > 45.0 с → True
    for i in range(5):
        store.record_attempt(site="s_slow", device="dev2", outcome="ok", offers=2, fetch_seconds=50.0, ts=now - 50 + i)
    assert hedge_risky("s_slow", "dev2", settings, now=now) is True

    # 5. При settings.hedge=False всегда False
    settings_no_hedge = Settings(sites=[], storage_dir=tmp_path, metrics=store, hedge=False)
    assert hedge_risky("s_slow", "dev2", settings_no_hedge, now=now) is False

    # 6. Ошибка чтения / поврежденная база → False
    assert hedge_risky("unknown", "unknown", settings, now=now) is False


# ============================================================================
# 2. ХЕДЖ ВЫИГРЫВАЕТ БЫСТРЫЙ, ПРОИГРАВШИЙ ОТМЕНЁН, ТЕЛЕФОН ОСВОБОЖДЁН
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_fast_wins_loser_cancelled_phone_freed(tmp_path, monkeypatch):
    """Хедж выигрывает быстрый:

    Основной dev1 рискованный (fail_share >= 0.15) и медленный (5.0 с).
    Хедж на свободном dev2 быстрый (0.02 с).
    Хедж возвращает офферы первым, dev1 отменяется.
    Оба телефона освобождаются.
    В attempts запись role='hedge'.
    """
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site1.test", phone="dev1,dev2", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
    )
    now = time.time()

    # Делаем пару (site1, dev1) рискованной: 5 попыток, 2 ошибки (40% >= 15%)
    for i in range(3):
        store.record_attempt(site="site1", device="dev1", outcome="ok", offers=1, fetch_seconds=2.0, ts=now - 50 + i)
    for i in range(2):
        store.record_attempt(site="site1", device="dev1", outcome="error", offers=0, fetch_seconds=2.0, ts=now - 20 + i)

    def p1(snap, q):
        return [Offer(site="site1", price=5500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    dev1_cancelled = False

    async def handler(request: httpx.Request):
        nonlocal dev1_cancelled
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        if "dev1" in url:
            try:
                await asyncio.sleep(5.0)
                return httpx.Response(200, json={"url": url, "text": "dev1_text", "finished": True})
            except asyncio.CancelledError:
                dev1_cancelled = True
                raise
        elif "dev2" in url:
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"url": url, "text": "dev2_text", "finished": True})
        return httpx.Response(404)

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 1
    res = results[0]
    assert res.ok is True
    assert res.device == "dev2"
    assert len(res.offers) == 1
    assert res.offers[0].price == 5500

    # Проигравший dev1 был отменён
    assert dev1_cancelled is True

    # Оба телефона освобождены
    assert "dev1" not in _busy_devices
    assert "dev2" not in _busy_devices

    # В attempts записана хедж-копия с role='hedge'
    assert wait_metrics_io()  # попытки пишутся без ожидания: дождаться записи
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attempts WHERE site = 'site1' AND role = 'hedge'").fetchall()
        assert len(rows) == 1
        assert rows[0]["device"] == "dev2"
        assert rows[0]["outcome"] == "ok"


# ============================================================================
# 3. ХЕДЖ НЕ СТАРТУЕТ БЕЗ СВОБОДНОГО ТЕЛЕФОНА
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_does_not_start_without_free_phone(tmp_path, monkeypatch):
    """Если пара рискованная, но второй телефон занят — хедж не стартует."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site1.test", phone="dev1,dev2", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
    )
    now = time.time()

    # Рискованная пара
    for i in range(5):
        store.record_attempt(site="site1", device="dev1", outcome="error", offers=0, fetch_seconds=2.0, ts=now - 50 + i)

    # dev2 уже занят чем-то другим!
    await acquire(["dev2"])

    def p1(snap, q):
        return [Offer(site="site1", price=5500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    calls = []

    async def handler(request: httpx.Request):
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        calls.append(url)
        return httpx.Response(200, json={"url": url, "text": "dev1_text", "finished": True})

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].device == "dev1"

    # Запрос был только к dev1, dev2 не вызывался
    assert all("dev1" in c for c in calls)

    # В attempts нет role='hedge'
    assert wait_metrics_io()  # попытки пишутся без ожидания: дождаться записи
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        hedge_rows = conn.execute("SELECT * FROM attempts WHERE site = 'site1' AND role = 'hedge'").fetchall()
        assert len(hedge_rows) == 0

    _busy_devices.discard("dev2")


# ============================================================================
# 4. ХЕДЖ НЕ СТАРТУЕТ ДЛЯ НЕРИСКОВАННОЙ ПАРЫ
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_does_not_start_for_non_risky_pair(tmp_path, monkeypatch):
    """Для надёжной быстрой пары хедж не запускается даже при свободном телефоне."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site1.test", phone="dev1,dev2", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
    )
    now = time.time()

    # Надёжная пара: 5 успешных быстрых попыток
    for i in range(5):
        store.record_attempt(site="site1", device="dev1", outcome="ok", offers=3, fetch_seconds=2.0, ts=now - 50 + i)

    def p1(snap, q):
        return [Offer(site="site1", price=5500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    calls = []

    async def handler(request: httpx.Request):
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        calls.append(url)
        return httpx.Response(200, json={"url": url, "text": "dev1_text", "finished": True})

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].device == "dev1"

    # Только dev1 вызывался
    assert len(calls) == 1
    assert "dev1" in calls[0]


# ============================================================================
# 5. НЕ БОЛЬШЕ 1 НА САЙТ И 2 НА ПОИСК
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_limits_max_1_per_site_and_2_per_search(tmp_path, monkeypatch):
    """Лимиты: 1 хедж на сайт за поиск, не более HEDGE_MAX_PER_SEARCH=2 на поиск."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    # 3 сайта, у каждого своя рискованная пара и свой свободный запасной телефон
    s1 = SiteConfig("s1", "https://s1.test", phone="d1,d4", parser="p1")
    s2 = SiteConfig("s2", "https://s2.test", phone="d2,d5", parser="p1")
    s3 = SiteConfig("s3", "https://s3.test", phone="d3,d6", parser="p1")

    settings = Settings(
        sites=[s1, s2, s3],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
        live_assignment=False,
        hedge_max_per_search=2,
    )
    now = time.time()

    # Все три пары рискованные
    for s_name, d_name in [("s1", "d1"), ("s2", "d2"), ("s3", "d3")]:
        for i in range(5):
            store.record_attempt(site=s_name, device=d_name, outcome="error", offers=0, fetch_seconds=2.0, ts=now - 50 + i)

    def p1(snap, q):
        return [Offer(site="any", price=5000, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    hedges_called: list[str] = []

    async def handler(request: httpx.Request):
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        for h_dev in ("d4", "d5", "d6"):
            if h_dev in url:
                hedges_called.append(h_dev)
                await asyncio.sleep(0.02)
                return httpx.Response(200, json={"url": url, "text": "text", "finished": True})
        # Основные копии медленные, чтобы хеджи побеждали
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={"url": url, "text": "text", "finished": True})

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 3

    # Ровно 2 сайта запустили хедж (лимит HEDGE_MAX_PER_SEARCH=2)
    assert wait_metrics_io()  # попытки пишутся без ожидания: дождаться записи
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        hedge_rows = conn.execute("SELECT * FROM attempts WHERE role = 'hedge'").fetchall()
        assert len(hedge_rows) == 2


# ============================================================================
# 6. HEDGE=0 — СТАРОЕ ПОВЕДЕНИЕ
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_flag_zero_old_behavior(tmp_path, monkeypatch):
    """При HEDGE=0 (settings.hedge=False) хедж полностью отключен."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site1.test", phone="dev1,dev2", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
        hedge=False,  # Отключен!
    )
    now = time.time()

    # Рискованная пара
    for i in range(5):
        store.record_attempt(site="site1", device="dev1", outcome="error", offers=0, fetch_seconds=2.0, ts=now - 50 + i)

    def p1(snap, q):
        return [Offer(site="site1", price=5500, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    calls = []

    async def handler(request: httpx.Request):
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        calls.append(url)
        return httpx.Response(200, json={"url": url, "text": "dev1_text", "finished": True})

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 1
    assert results[0].device == "dev1"
    assert len(calls) == 1
    assert "dev1" in calls[0]


# ============================================================================
# 7. ОШИБКА ОСНОВНОЙ КОПИИ → РЕЗУЛЬТАТ ОТ ХЕДЖА
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_primary_error_hedge_result_used(tmp_path, monkeypatch):
    """Если первая копия завершилась ошибкой, ждём вторую (хедж), и её результат отдаётся в выдачу."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site1.test", phone="dev1,dev2", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
    )
    now = time.time()

    # Рискованная пара
    for i in range(5):
        store.record_attempt(site="site1", device="dev1", outcome="error", offers=0, fetch_seconds=2.0, ts=now - 50 + i)

    def p1(snap, q):
        if "dev1" in snap.text:
            return []
        return [Offer(site="site1", price=7700, airline="AFL")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    async def handler(request: httpx.Request):
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        if "dev1" in url:
            # Основная копия сразу возвращает 502
            return httpx.Response(502, text="bad gateway")
        elif "dev2" in url:
            # Хедж-копия завершается успешно
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"url": url, "text": "dev2_content", "finished": True})
        return httpx.Response(404)

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 1
    res = results[0]
    assert res.ok is True
    assert res.device == "dev2"
    assert len(res.offers) == 1
    assert res.offers[0].price == 7700

    # Оба телефона свободны
    assert "dev1" not in _busy_devices
    assert "dev2" not in _busy_devices


# ============================================================================
# 8. ХЕДЖ ПО p50 ДЛЯ МЕДЛЕННЫХ ПАР
# ============================================================================


@pytest.mark.asyncio
async def test_hedge_p50_delay_for_slow_pairs(tmp_path, monkeypatch):
    """Для медленной пары (fail_share < 0.15, p90 > 45) хедж стартует через p50 пары,

    если основной не ответил вовремя.
    """
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig("site1", "https://site1.test", phone="dev1,dev2", parser="p1")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        speed_routing=False,
        live_safety_seconds=90.0,
    )
    now = time.time()

    # Медленная пара: 5 успешных попыток, p50 = 0.1 с, p90 = 50.0 с > 45.0 с
    store.record_attempt(site="site1", device="dev1", outcome="ok", offers=1, fetch_seconds=0.1, ts=now - 50)
    store.record_attempt(site="site1", device="dev1", outcome="ok", offers=1, fetch_seconds=0.1, ts=now - 40)
    store.record_attempt(site="site1", device="dev1", outcome="ok", offers=1, fetch_seconds=0.1, ts=now - 30)
    store.record_attempt(site="site1", device="dev1", outcome="ok", offers=1, fetch_seconds=50.0, ts=now - 20)
    store.record_attempt(site="site1", device="dev1", outcome="ok", offers=1, fetch_seconds=50.0, ts=now - 10)

    assert hedge_risky("site1", "dev1", settings, now=now) is True

    def p1(snap, q):
        return [Offer(site="site1", price=4200, airline="SU")]

    monkeypatch.setitem(PARSERS, "p1", p1)

    dev2_called = False

    async def handler(request: httpx.Request):
        nonlocal dev2_called
        url = str(request.url)
        if "fetch_jobs" in url:
            return httpx.Response(404)
        if "dev1" in url:
            # dev1 завис на 5 секунд (> p50 = 0.1 c)
            await asyncio.sleep(5.0)
            return httpx.Response(200, json={"url": url, "text": "dev1_text", "finished": True})
        elif "dev2" in url:
            dev2_called = True
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"url": url, "text": "dev2_text", "finished": True})
        return httpx.Response(404)

    _mock_async_client(monkeypatch, handler)

    q = Query("MOW", "LED", date(2026, 12, 10))
    results = await search(q, settings, now=now)

    assert len(results) == 1
    assert results[0].device == "dev2"
    assert dev2_called is True
