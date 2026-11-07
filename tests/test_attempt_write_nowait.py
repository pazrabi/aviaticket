"""КТ1, работа 5: готовые цены сайта не ждут записи попытки в метрики.

Запись попытки (attempts) уходит в пул метрик без ожидания: под 100 поисками этот пул занят
предохранителем, хеджами и раскладкой, и карточка сайта не должна стоять в его очереди.
Роль попытки (primary / retry / hedge) и её время считаются сразу, в цикле событий, как в прежней
синхронной записи. Ошибка записи попадает в лог, а цены сайта всё равно возвращаются.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import logging
import sqlite3
import time

import pytest

from avia import metrics as metrics_mod
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.dispatcher import reset_fuse_state
from avia.metrics import MetricsStore
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import _from_browser
from avia.speed import reset_speed_tables

PARSER_NAME = "nowait_parser"
BUSY_SECONDS = 2.0
MAX_RETURN_SECONDS = 0.1


def _drain_metrics_io(timeout: float = 15.0) -> None:
    """Дождаться записей, отданных пулу метрик без ожидания (до правки запись ждали на месте)."""
    wait = getattr(metrics_mod, "wait_metrics_io", None)
    if wait is not None:
        assert wait(timeout), "пул метрик не дописал попытки"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_fuse_state()
    reset_speed_tables()
    yield
    _drain_metrics_io()
    reset_fuse_state()
    reset_speed_tables()


def _occupy_metrics_pools() -> list:
    """Занять каждый поток каждого пула метрик задачей, которая спит BUSY_SECONDS (как длинный запрос)."""
    pools = [v for v in vars(metrics_mod).values() if isinstance(v, ThreadPoolExecutor)]
    assert pools, "в avia.metrics нет пула потоков"
    blockers = []
    for pool in pools:
        for _ in range(pool._max_workers):
            blockers.append(pool.submit(time.sleep, BUSY_SECONDS))
    # все потоки пулов уже спят
    time.sleep(0.05)
    return blockers


def _setup(tmp_path, monkeypatch, fetch) -> tuple[SiteConfig, Settings, Query]:
    MetricsStore(tmp_path / "metrics.sqlite")._ensure_schema()

    def parser(snap, q):
        return [Offer(site="nowait", price=4200, airline="SU")]

    monkeypatch.setitem(PARSERS, PARSER_NAME, parser)
    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fetch)
    site = SiteConfig("nowait_site", "https://nowait.test/{origin}/{destination}", phone="dev1,dev2", parser=PARSER_NAME)
    # metrics=None, как в проде: попытку пишет MetricsStore по storage_dir
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        droidhub_url="https://hub.test",
        droidhub_token="t",
        price_timeline=False,
        speed_routing=False,
    )
    return site, settings, Query("MOW", "AER", date(2026, 12, 10))


def _attempt_rows(tmp_path, search_id: str) -> list[sqlite3.Row]:
    with sqlite3.connect(tmp_path / "metrics.sqlite") as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT device, outcome, role, offers, ts FROM attempts WHERE search_id = ? ORDER BY id ASC",
            (search_id,),
        ).fetchall()


async def _ok_fetch(settings, cands, url, **kwargs):
    stats = kwargs.get("stats")
    if stats is not None:
        stats["device"] = list(cands)[0]
        stats["fetch_seconds"] = 1.0
    return Snapshot(url=url, text='[{"price": 4200}]'), True


@pytest.mark.asyncio
async def test_offers_return_while_metrics_pool_is_busy(tmp_path, monkeypatch):
    """Пул метрик занят (два потока спят по 2 с) → _from_browser с ценами возвращается быстрее 100 мс."""
    site, settings, q = _setup(tmp_path, monkeypatch, _ok_fetch)
    # прогрев: импорты, пул разбора страниц
    await _from_browser(site, q, settings, priority=0, stats={}, search_id="nowait_warm")
    _drain_metrics_io()

    blockers = _occupy_metrics_pools()
    t0 = time.perf_counter()
    offers, _, _ = await _from_browser(site, q, settings, priority=0, stats={}, search_id="nowait_busy")
    elapsed = time.perf_counter() - t0

    assert [o.price for o in offers] == [4200]
    assert elapsed < MAX_RETURN_SECONDS, f"_from_browser ждал запись попытки {elapsed * 1000:.0f} мс"
    # попытка ещё стоит в очереди пула — и записывается после него
    assert not all(b.done() for b in blockers)
    _drain_metrics_io()
    rows = _attempt_rows(tmp_path, "nowait_busy")
    assert [(r["device"], r["outcome"], r["role"], r["offers"]) for r in rows] == [("dev1", "ok", "primary", 1)]


@pytest.mark.asyncio
async def test_attempt_role_and_time_are_taken_in_loop(tmp_path, monkeypatch):
    """Капча на первом телефоне, цены на втором: роли primary и retry, время — момент попытки."""

    async def fetch(settings, cands, url, **kwargs):
        dev = list(cands)[0]
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = dev
            stats["fetch_seconds"] = 1.0
        if dev == "dev1":
            return Snapshot(url=url, text="Blocked by captcha", blocked=True), True
        return Snapshot(url=url, text='[{"price": 4200}]'), True

    site, settings, q = _setup(tmp_path, monkeypatch, fetch)
    await _from_browser(site, q, settings, priority=0, stats={}, search_id="nowait_warm")
    _drain_metrics_io()
    reset_speed_tables()

    _occupy_metrics_pools()
    tracker = [0]
    offers, _, _ = await _from_browser(
        site, q, settings, priority=0, stats={}, search_id="nowait_roles", attempt_tracker=tracker
    )
    returned_at = time.time()

    assert [o.price for o in offers] == [4200]
    # роль посчитана в цикле событий, до записи: счётчик попыток сайта уже 2
    assert tracker == [2]
    _drain_metrics_io()
    rows = _attempt_rows(tmp_path, "nowait_roles")
    assert [(r["device"], r["outcome"], r["role"]) for r in rows] == [
        ("dev1", "captcha", "primary"),
        ("dev2", "ok", "retry"),
    ]
    # время попытки — когда она закончилась, а не когда пул дошёл до записи (через ~2 с)
    assert all(r["ts"] <= returned_at + 0.5 for r in rows), [r["ts"] - returned_at for r in rows]


@pytest.mark.asyncio
async def test_failed_attempt_write_is_logged_and_offers_kept(tmp_path, monkeypatch, caplog):
    """Запись попытки упала → ошибка в логе, а цены сайта всё равно возвращены."""
    site, settings, q = _setup(tmp_path, monkeypatch, _ok_fetch)

    def broken_record_attempt(self, *args, **kwargs):
        raise RuntimeError("диск переполнен")

    monkeypatch.setattr(MetricsStore, "record_attempt", broken_record_attempt)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _from_browser(site, q, settings, priority=0, stats={}, search_id="nowait_broken")
        _drain_metrics_io()

    assert [o.price for o in offers] == [4200]
    failures = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and r.exc_info and "диск переполнен" in str(r.exc_info[1])
    ]
    assert failures, [r.getMessage() for r in caplog.records]
    assert _attempt_rows(tmp_path, "nowait_broken") == []
