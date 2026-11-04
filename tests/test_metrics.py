"""Тесты хранилища метрик парсинга (MetricsStore) и интеграции в Bot."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from avia.bot import Bot
from avia.config import Settings
from avia.dialog import SearchRequest
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult


def test_record_run_and_summary_total(tmp_path):
    """record_run корректно сохраняет метрики, а summary строит сводку по сайтам и '_total'."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    t0 = 1000.0

    # siteA: 1 успешный (2 оффера, 1.5 сек), 1 частичный (1 оффер, 2.0 сек), 1 ошибка капчи (0.5 сек)
    store.record_run("user", "siteA", ok=True, partial=False, captcha=False, error=None, offers=2, elapsed=1.5, ts=t0 + 10)
    store.record_run("user", "siteA", ok=True, partial=True, captcha=False, error=None, offers=1, elapsed=2.0, ts=t0 + 20)
    store.record_run("user", "siteA", ok=False, partial=False, captcha=True, error="капча", offers=0, elapsed=0.5, ts=t0 + 30)

    # siteB: 1 успешный (3 оффера, 3.0 сек) с источником 'sub'
    store.record_run("sub", "siteB", ok=True, partial=False, captcha=False, error=None, offers=3, elapsed=3.0, ts=t0 + 40)

    # Сводка за всё время (все источники)
    summ = store.summary(since_ts=t0)
    assert "siteA" in summ
    assert "siteB" in summ
    assert "_total" in summ

    a = summ["siteA"]
    assert a["runs"] == 3
    assert a["ok"] == 2
    assert a["partial"] == 1
    assert a["captcha"] == 1
    assert a["failed"] == 1
    assert a["avg_elapsed"] == round((1.5 + 2.0 + 0.5) / 3, 2)
    assert a["avg_offers"] == round((2 + 1 + 0) / 3, 2)
    assert a["last_error"] == "капча"
    assert a["last_error_ts"] == t0 + 30

    b = summ["siteB"]
    assert b["runs"] == 1
    assert b["ok"] == 1
    assert b["failed"] == 0
    assert b["last_error"] is None

    tot = summ["_total"]
    assert tot["runs"] == 4
    assert tot["ok"] == 3
    assert tot["partial"] == 1
    assert tot["captcha"] == 1
    assert tot["failed"] == 1
    assert tot["avg_elapsed"] == round((1.5 + 2.0 + 0.5 + 3.0) / 4, 2)
    assert tot["avg_offers"] == round((2 + 1 + 0 + 3) / 4, 2)
    assert tot["last_error"] == "капча"
    assert tot["last_error_ts"] == t0 + 30

    # Фильтрация по источнику 'sub'
    sub_summ = store.summary(since_ts=t0, source="sub")
    assert "siteA" not in sub_summ
    assert "siteB" in sub_summ
    assert sub_summ["siteB"]["runs"] == 1
    assert sub_summ["_total"]["runs"] == 1


def test_record_results_and_captcha_detection(tmp_path):
    """record_results определяет капчу как по полю captcha, так и по слову 'капч' в ошибке."""
    store = MetricsStore(tmp_path / "metrics.sqlite")

    offer1 = Offer(site="site1", price=1000)
    offer2 = Offer(site="site1", price=1200)

    r1 = SiteResult(site="site1", ok=True, offers=[offer1, offer2], elapsed=1.2, partial=True)
    r2 = SiteResult(site="site2", ok=False, offers=[], error="сайт показал капчу", elapsed=0.8, captcha=False)
    r3 = SiteResult(site="site3", ok=False, offers=[], error="403 Forbidden", elapsed=0.4, captcha=True)
    r4 = SiteResult(site="site4", ok=False, offers=[], error="таймаут сети", elapsed=5.0, captcha=False)

    store.record_results(source="user", results=[r1, r2, r3, r4], ts=500.0)

    summ = store.summary(since_ts=0.0)
    assert summ["site1"]["runs"] == 1
    assert summ["site1"]["ok"] == 1
    assert summ["site1"]["partial"] == 1
    assert summ["site1"]["avg_offers"] == 2.0

    # site2 определил капчу по тексту ошибки без учёта регистра
    assert summ["site2"]["captcha"] == 1
    # site3 определил капчу по явному полю captcha=True
    assert summ["site3"]["captcha"] == 1
    # site4 не капча
    assert summ["site4"]["captcha"] == 0

    assert summ["_total"]["captcha"] == 2


def test_recent_errors_and_truncation(tmp_path):
    """recent_errors возвращает ошибки по убыванию времени и обрезает текст до 300 символов."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    long_err = "x" * 500

    store.record_run("user", "s1", ok=False, partial=False, captcha=False, error="err1", offers=0, elapsed=1.0, ts=100.0)
    store.record_run("sub", "s2", ok=False, partial=False, captcha=False, error=long_err, offers=0, elapsed=1.0, ts=200.0)
    store.record_run("bg", "s3", ok=True, partial=False, captcha=False, error=None, offers=2, elapsed=1.0, ts=300.0)

    errors = store.recent_errors(limit=10, since_ts=50.0)
    assert len(errors) == 2
    # Сначала самая свежая
    assert errors[0]["site"] == "s2"
    assert errors[0]["source"] == "sub"
    assert len(errors[0]["error"]) == 300
    assert errors[0]["ts"] == 200.0

    assert errors[1]["site"] == "s1"
    assert errors[1]["error"] == "err1"

    # Проверка лимита
    limited = store.recent_errors(limit=1)
    assert len(limited) == 1
    assert limited[0]["site"] == "s2"


def test_counts_by_source(tmp_path):
    """counts_by_source подсчитывает число записей по источникам."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    store.record_run("user", "s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0, ts=100.0)
    store.record_run("user", "s2", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0, ts=100.0)
    store.record_run("sub", "s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0, ts=100.0)
    store.record_run("bg", "s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0, ts=100.0)

    counts = store.counts_by_source(since_ts=50.0)
    assert counts == {"user": 2, "sub": 1, "bg": 1}

    # За период после ts=150 ничего нет
    assert store.counts_by_source(since_ts=150.0) == {}


def test_prune(tmp_path):
    """prune удаляет записи старше keep_days."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    now = 20_000_000.0
    day_s = 86400.0

    # 100 дней назад (должен удалиться при keep_days=90)
    store.record_run("user", "s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0, ts=now - 100 * day_s)
    # 50 дней назад (должен остаться)
    store.record_run("user", "s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0, ts=now - 50 * day_s)

    deleted = store.prune(keep_days=90, now=now)
    assert deleted == 1

    summ = store.summary(since_ts=0.0)
    assert summ["_total"]["runs"] == 1


def test_recording_error_does_not_raise(tmp_path, monkeypatch):
    """Ошибка записи метрик не должна бросать исключение наружу."""
    store = MetricsStore(tmp_path / "metrics.sqlite")

    # Имитируем сбой подключения/записи
    monkeypatch.setattr(store, "_connect", MagicMock(side_effect=RuntimeError("disk full")))

    # Не должно упасть
    store.record_run("user", "s1", ok=True, partial=False, captcha=False, error=None, offers=1, elapsed=1.0)
    store.record_results("user", [SiteResult(site="s1", ok=True, offers=[])])


@pytest.mark.asyncio
async def test_bot_run_search_records_metrics(tmp_path, monkeypatch):
    """Bot._run_search записывает метрики с source='user'."""
    settings = Settings(
        sites=[],
        tg_token="dummy_token_123",
        tg_allowed_ids={1},
        storage_dir=tmp_path / "storage",
    )
    bot = Bot(settings)

    mock_metrics = MagicMock()
    bot.metrics = mock_metrics

    fake_results = [
        SiteResult(site="siteX", ok=True, offers=[], elapsed=1.0),
    ]

    async def fake_search(q, s):
        return fake_results

    monkeypatch.setattr("avia.bot.search", fake_search)
    monkeypatch.setattr(bot, "call", AsyncMock(return_value={"message_id": 10}))
    monkeypatch.setattr(bot, "send", AsyncMock(return_value={"message_id": 11}))

    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    req = SearchRequest(query=q, baggage_mode="any")
    await bot._run_search(chat_id=1, req=req)

    mock_metrics.record_results.assert_called_once_with("user", fake_results)


@pytest.mark.asyncio
async def test_bot_bg_and_scheduler_search_record_metrics(tmp_path, monkeypatch):
    """_bg_search и _scheduler_search записывают метрики с 'bg' и 'sub'."""
    settings = Settings(
        sites=[],
        tg_token="dummy_token_123",
        tg_allowed_ids={1},
        storage_dir=tmp_path / "storage",
    )
    bot = Bot(settings)

    mock_metrics = MagicMock()
    bot.metrics = mock_metrics

    fake_results = [
        SiteResult(site="siteY", ok=True, offers=[], elapsed=0.5),
    ]

    async def fake_search(q, s):
        return fake_results

    monkeypatch.setattr("avia.bot.search", fake_search)
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    res_bg = await bot._bg_search(q)
    assert res_bg == fake_results
    mock_metrics.record_results.assert_any_call("bg", fake_results)

    res_sub = await bot._scheduler_search(q)
    assert res_sub == fake_results
    mock_metrics.record_results.assert_any_call("sub", fake_results)


def test_metrics_summary_by_device_and_by_site_device(tmp_path):
    """summary_by_device и summary_by_site_device строят сводки по устройствам."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    r1 = SiteResult(site="tutu", ok=True, offers=[], elapsed=1.5, device="realme", queue_wait=0.2, fetch_seconds=1.3)
    r2 = SiteResult(site="tutu", ok=False, offers=[], elapsed=1.0, error="капча", device="realme", queue_wait=0.1, fetch_seconds=0.9)
    r3 = SiteResult(site="yandex", ok=True, offers=[], elapsed=2.0, device="redmi", queue_wait=0.0, fetch_seconds=2.0)
    store.record_results("user", [r1, r2, r3], ts=100.0)

    by_dev = store.summary_by_device(since_ts=50.0)
    assert "realme" in by_dev
    assert by_dev["realme"]["runs"] == 2
    assert by_dev["realme"]["ok"] == 1
    assert by_dev["realme"]["captcha"] == 1
    assert by_dev["realme"]["avg_fetch"] == 1.1
    assert by_dev["realme"]["avg_queue_wait"] == 0.15

    assert "redmi" in by_dev
    assert by_dev["redmi"]["runs"] == 1
    assert by_dev["redmi"]["ok"] == 1

    by_site_dev = store.summary_by_site_device(since_ts=50.0)
    assert ("tutu", "realme") in by_site_dev
    assert by_site_dev[("tutu", "realme")]["runs"] == 2
    assert ("yandex", "redmi") in by_site_dev
    assert by_site_dev[("yandex", "redmi")]["runs"] == 1


def test_watch_stats_and_parser_watch_state(tmp_path):
    """watch_stats агрегирует запуски, а get_watch_state / set_watch_state сохраняют состояние."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    t0 = 500.0

    store.record_run("user", "siteA", ok=True, partial=False, captcha=False, error=None, offers=2, elapsed=1.0, ts=t0 + 10)
    store.record_run("user", "siteA", ok=False, partial=True, captcha=False, error="err", offers=0, elapsed=1.0, ts=t0 + 20)
    store.record_run("user", "siteB", ok=True, partial=True, captcha=False, error=None, offers=1, elapsed=1.0, ts=t0 + 30)

    stats = store.watch_stats(since_ts=t0)
    assert stats["siteA"]["runs"] == 2
    assert stats["siteA"]["ok"] == 1
    assert stats["siteA"]["partial"] == 1

    assert stats["siteB"]["runs"] == 1
    assert stats["siteB"]["ok"] == 1
    assert stats["siteB"]["partial"] == 1

    # Проверка состояния parser_watch_state
    assert store.get_watch_state("siteA") is None

    store.set_watch_state("siteA", last_status="сломан", last_alert_ts=t0 + 50, last_alert_status="сломан")
    st = store.get_watch_state("siteA")
    assert st is not None
    assert st["site"] == "siteA"
    assert st["last_status"] == "сломан"
    assert st["last_alert_ts"] == t0 + 50
    assert st["last_alert_status"] == "сломан"

    # Обновление состояния (upsert)
    store.set_watch_state("siteA", last_status="ок", last_alert_ts=t0 + 100, last_alert_status="ок")
    st2 = store.get_watch_state("siteA")
    assert st2["last_status"] == "ок"
    assert st2["last_alert_ts"] == t0 + 100
    assert st2["last_alert_status"] == "ок"

