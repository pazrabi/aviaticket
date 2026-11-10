"""Тесты работы с заданиями хаба fetch_jobs и интеграции с pipeline."""
from __future__ import annotations

import asyncio
from datetime import date
import json
from pathlib import Path
import sqlite3
import time

import httpx
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.droidhub import (
    DroidhubError,
    JobsUnsupported,
    _fetch_device_job,
    _reset_pool,
    fetch_page_snapshot_any,
    reset_jobs_supported,
)
import avia.droidhub as droidhub
from avia.metrics import MetricsStore, wait_metrics_io
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import _from_browser, search
import avia.pipeline as pipeline


_ORIG_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _reset_droidhub_state():
    """Сбрасывает состояние пула и флагов droidhub перед и после каждого теста."""
    _reset_pool()
    reset_jobs_supported()
    orig_interval = droidhub.POLL_MIN_INTERVAL
    orig_del_timeout = droidhub.DELETE_TIMEOUT
    orig_parser_interval = pipeline.PARSER_MIN_INTERVAL
    droidhub.POLL_MIN_INTERVAL = 0.0
    droidhub.DELETE_TIMEOUT = 1.0
    pipeline.PARSER_MIN_INTERVAL = 0.0
    yield
    _reset_pool()
    reset_jobs_supported()
    droidhub.POLL_MIN_INTERVAL = orig_interval
    droidhub.DELETE_TIMEOUT = orig_del_timeout
    pipeline.PARSER_MIN_INTERVAL = orig_parser_interval


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "avia.droidhub.httpx.AsyncClient",
        lambda **kwargs: _ORIG_ASYNC_CLIENT(
            transport=transport,
            **{k: v for k, v in kwargs.items() if k != "transport"},
        ),
    )
    return transport


# ============================================================================
# (в) _fetch_device_job: порции ответов, evaluate, finish, Snapshot, info
# ============================================================================

@pytest.mark.asyncio
async def test_fetch_device_job_portions_and_evaluate_finish(monkeypatch):
    """(в) _fetch_device_job: поддельное задание отдаёт ответы по частям с t,

    evaluate останавливает на второй порции → вызван finish, итоговый Snapshot
    содержит накопленные тела, payload_times и text из finish, finished=False,
    info.stop_reason='stable'; wait_for_seen → finished=True.
    """
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        calls.append((request.method, url_str))

        if request.method == "POST" and url_str.endswith("/api/devices/dev1/fetch_jobs"):
            return httpx.Response(200, json={"job_id": "job_123"})

        if request.method == "GET" and "/api/fetch_jobs/job_123" in url_str:
            if "since=0" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "responses": [{"i": 0, "t": 2.1, "url": "https://site/api/1", "body": "chunk_one"}],
                        "next": 1,
                        "state": "running",
                        "wait_for_seen": False,
                        "network_quiet": False,
                        "elapsed": 2.2,
                        "captcha": False,
                        "error": None,
                    },
                )
            if "since=1" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "responses": [{"i": 1, "t": 4.8, "url": "https://site/api/2", "body": "chunk_two"}],
                        "next": 2,
                        "state": "running",
                        "wait_for_seen": False,
                        "network_quiet": False,
                        "elapsed": 5.0,
                        "captcha": False,
                        "error": None,
                    },
                )

        if request.method == "POST" and url_str.endswith("/api/fetch_jobs/job_123/finish"):
            return httpx.Response(
                200,
                json={
                    "url": "https://site.test/result",
                    "title": "Результаты",
                    "text": "Страница с рейсами",
                    "truncated": False,
                    "captcha": False,
                    "finished": True,
                    "seconds": 5.0,
                    "woke": False,
                    "t_first_match": 2.1,
                    "json_responses": [],
                },
            )

        return httpx.Response(404, text="Not Found")

    _mock_client(monkeypatch, handler)

    eval_calls = 0

    async def evaluate_cb(snap: Snapshot, hub_state: dict):
        nonlocal eval_calls
        eval_calls += 1
        if len(snap.json_payloads) == 1:
            return None
        if len(snap.json_payloads) == 2:
            evaluate_cb.t_first_offer = 2.1
            evaluate_cb.t_last_improve = 4.8
            return "stable"
        return None

    evaluate_cb.t_first_offer = None
    evaluate_cb.t_last_improve = None

    snap, finished, info = await _fetch_device_job(
        settings,
        "dev1",
        "https://site.test/search",
        wait_seconds=40,
        evaluate=evaluate_cb,
    )

    # Проверяем последовательность вызовов
    assert any(m == "POST" and "finish" in u for m, u in calls)
    assert not any(m == "DELETE" for m, u in calls)

    # Проверяем итоговый снимок
    assert snap.url == "https://site.test/result"
    assert snap.text == "Страница с рейсами"
    assert snap.json_payloads == [
        ("https://site/api/1", "chunk_one"),
        ("https://site/api/2", "chunk_two"),
    ]
    assert snap.payload_times == [2.1, 4.8]
    assert snap.blocked is False
    assert snap.woke is False
    assert snap.t_first_match == 2.1

    # Для 'stable' выдача считается неполной -> finished = False
    assert finished is False
    assert info["stop_reason"] == "stable"
    assert info["elapsed"] == 5.0
    assert info["t_first_offer"] == 2.1
    assert info["t_last_improve"] == 4.8


@pytest.mark.asyncio
async def test_fetch_device_job_wait_for_seen_finished_true(monkeypatch):
    """(в) Причина 'done' (wait_for_seen) даёт finished=True."""
    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="secret")

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/fetch_jobs" in url_str and not url_str.endswith("/finish"):
            return httpx.Response(200, json={"job_id": "job_done_1"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "responses": [{"i": 0, "t": 3.0, "url": "https://site/api", "body": "{}"}],
                    "next": 1,
                    "state": "running",
                    "wait_for_seen": True,
                    "network_quiet": True,
                    "elapsed": 3.5,
                    "captcha": False,
                    "error": None,
                },
            )
        if request.method == "POST" and url_str.endswith("/finish"):
            return httpx.Response(
                200,
                json={
                    "url": "https://site.test",
                    "text": "Готово",
                    "captcha": False,
                    "finished": True,
                    "seconds": 3.5,
                    "json_responses": [],
                },
            )
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)

    async def eval_done(snap, hub_state):
        if hub_state.get("wait_for_seen"):
            return "done"
        return None

    snap, finished, info = await _fetch_device_job(
        settings,
        "dev1",
        "https://site.test",
        evaluate=eval_done,
    )
    assert finished is True
    assert info["stop_reason"] == "done"


# ============================================================================
# (г) Исключение в evaluate и отмена задачи вызывают DELETE
# ============================================================================

@pytest.mark.asyncio
async def test_fetch_device_job_deletes_on_evaluate_exception(monkeypatch):
    """(г) При исключении в evaluate гарантированно вызывается DELETE."""
    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="secret")
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/fetch_jobs" in url_str:
            return httpx.Response(200, json={"job_id": "job_err_1"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "responses": [{"i": 0, "t": 1.0, "url": "u", "body": "b"}],
                    "next": 1,
                    "state": "running",
                    "wait_for_seen": False,
                    "network_quiet": False,
                    "elapsed": 1.0,
                    "captcha": False,
                    "error": None,
                },
            )
        if request.method == "DELETE" and "job_err_1" in url_str:
            deleted.append(url_str)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)

    async def failing_eval(snap, hub_state):
        raise RuntimeError("ошибка внутри evaluate")

    with pytest.raises(RuntimeError, match="ошибка внутри evaluate"):
        await _fetch_device_job(settings, "dev1", "https://site.test", evaluate=failing_eval)

    assert len(deleted) == 1
    assert "job_err_1" in deleted[0]


@pytest.mark.asyncio
async def test_fetch_device_job_deletes_on_cancellation(monkeypatch):
    """(г) При отмене задачи (asyncio.CancelledError) гарантированно вызывается DELETE."""
    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="secret")
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/fetch_jobs" in url_str:
            return httpx.Response(200, json={"job_id": "job_cancel_1"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "responses": [],
                    "next": 0,
                    "state": "running",
                    "wait_for_seen": False,
                    "network_quiet": False,
                    "elapsed": 0.5,
                    "captcha": False,
                    "error": None,
                },
            )
        if request.method == "DELETE" and "job_cancel_1" in url_str:
            deleted.append(url_str)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)

    poll_started = asyncio.Event()

    async def waiting_eval(snap, hub_state):
        poll_started.set()
        await asyncio.sleep(10.0)
        return None

    task = asyncio.create_task(
        _fetch_device_job(settings, "dev1", "https://site.test", evaluate=waiting_eval)
    )

    await poll_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(deleted) == 1
    assert "job_cancel_1" in deleted[0]


# ============================================================================
# (д) 404 на создание -> JobsUnsupported и откат на fetch_page, повторно не создаётся
# ============================================================================

@pytest.mark.asyncio
async def test_jobs_unsupported_fallback_and_process_flag(monkeypatch):
    """(д) 404 на создание → JobsUnsupported и откат на fetch_page,

    второй раз задание уже не создаётся (глобальный флаг).
    """
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        calls.append((request.method, url_str))

        if "/fetch_jobs" in url_str:
            # Старый хаб не имеет /fetch_jobs
            return httpx.Response(404, text="Not Found")

        if "/fetch_page" in url_str:
            return httpx.Response(
                200,
                json={
                    "url": "https://site.test",
                    "text": "Старый ответ fetch_page",
                    "captcha": False,
                    "finished": True,
                    "seconds": 15.0,
                    "json_responses": [],
                },
            )
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)

    async def dummy_eval(snap, state):
        return None

    # Первый вызов: пробует fetch_jobs -> 404 -> JobsUnsupported -> откат на fetch_page
    snap1, fin1 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://site.test",
        evaluate=dummy_eval,
    )
    assert snap1.text == "Старый ответ fetch_page"
    assert fin1 is True

    # Проверяем, что был 1 запрос к fetch_jobs и 1 к fetch_page
    jobs_calls = [c for c in calls if "fetch_jobs" in c[1]]
    page_calls = [c for c in calls if "fetch_page" in c[1]]
    assert len(jobs_calls) == 1
    assert len(page_calls) == 1

    # Второй вызов: больше НЕ пробует fetch_jobs вообще
    calls.clear()
    snap2, fin2 = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://site.test",
        evaluate=dummy_eval,
    )
    assert snap2.text == "Старый ответ fetch_page"
    assert len([c for c in calls if "fetch_jobs" in c[1]]) == 0
    assert len([c for c in calls if "fetch_page" in c[1]]) == 1


# ============================================================================
# (е) FAST_STOP=0 — задания не создаются
# ============================================================================

@pytest.mark.asyncio
async def test_fast_stop_disabled_does_not_create_jobs(monkeypatch):
    """(е) FAST_STOP=0 — задания не создаются."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=False,  # FAST_STOP=0
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        calls.append((request.method, url_str))
        if "/fetch_page" in url_str:
            return httpx.Response(
                200,
                json={
                    "url": "https://site.test",
                    "text": "Старый путь fetch_page",
                    "captcha": False,
                    "finished": True,
                    "seconds": 10.0,
                    "json_responses": [],
                },
            )
        return httpx.Response(500)

    _mock_client(monkeypatch, handler)

    async def dummy_eval(snap, state):
        return None

    snap, fin = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://site.test",
        evaluate=dummy_eval,
    )
    assert snap.text == "Старый путь fetch_page"
    # Ни одного вызова fetch_jobs
    assert not any("fetch_jobs" in c[1] for c in calls)
    assert any("fetch_page" in c[1] for c in calls)


# ============================================================================
# (ж) priority > 0 — задания не создаются
# ============================================================================

@pytest.mark.asyncio
async def test_priority_greater_than_zero_does_not_create_jobs(monkeypatch):
    """(ж) priority>0 — задания не создаются."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        fast_stop=True,
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        calls.append((request.method, url_str))
        if "/fetch_page" in url_str:
            return httpx.Response(
                200,
                json={
                    "url": "https://site.test",
                    "text": "Фоновый запрос",
                    "captcha": False,
                    "finished": True,
                    "seconds": 12.0,
                    "json_responses": [],
                },
            )
        return httpx.Response(500)

    _mock_client(monkeypatch, handler)

    async def dummy_eval(snap, state):
        return None

    # Приоритет 1 (подписки) или 2 (фон)
    snap, fin = await fetch_page_snapshot_any(
        settings,
        ["dev1"],
        "https://site.test",
        priority=1,
        evaluate=dummy_eval,
    )
    assert snap.text == "Фоновый запрос"
    assert not any("fetch_jobs" in c[1] for c in calls)
    assert any("fetch_page" in c[1] for c in calls)


# ============================================================================
# (з) record_attempt пишет stop_reason/t_first_offer/t_last_improve, миграция старой базы
# ============================================================================

def test_record_attempt_and_migration(tmp_path):
    """(з) record_attempt пишет stop_reason/t_first_offer/t_last_improve,

    миграция старой базы без этих колонок проходит успешно.
    """
    db_path = tmp_path / "legacy_metrics.sqlite"

    # Создаём «старую» базу с исходной схемой attempts без новых колонок
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE attempts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL NOT NULL,
                search_id     TEXT,
                site          TEXT NOT NULL,
                device        TEXT,
                source        TEXT NOT NULL,
                role          TEXT NOT NULL,
                outcome       TEXT NOT NULL,
                offers        INTEGER NOT NULL,
                fetch_seconds REAL,
                queue_wait    REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO attempts (ts, search_id, site, device, source, role, outcome, offers, fetch_seconds, queue_wait)
            VALUES (1000.0, 's_old', 'aviasales', 'dev_old', 'user', 'primary', 'ok', 3, 12.5, 0.4)
            """
        )

    # Инициализируем MetricsStore: должна примениться автоматическая миграция (ALTER TABLE ADD COLUMN)
    store = MetricsStore(db_path)

    # Записываем новую попытку с новыми колонками
    store.record_attempt(
        site="aviasales",
        device="dev_new",
        source="user",
        role="primary",
        outcome="partial",
        offers=5,
        fetch_seconds=8.5,
        queue_wait=0.2,
        search_id="s_new",
        stop_reason="stable",
        t_first_offer=3.1,
        t_last_improve=6.2,
    )

    # Проверяем записи в базе
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attempts ORDER BY id ASC").fetchall()
        assert len(rows) == 2

        # Старая запись: новые поля NULL
        r_old = rows[0]
        assert r_old["site"] == "aviasales"
        assert r_old["stop_reason"] is None
        assert r_old["t_first_offer"] is None
        assert r_old["t_last_improve"] is None

        # Новая запись: новые поля заполнены
        r_new = rows[1]
        assert r_new["device"] == "dev_new"
        assert r_new["outcome"] == "partial"
        assert r_new["stop_reason"] == "stable"
        assert r_new["t_first_offer"] == 3.1
        assert r_new["t_last_improve"] == 6.2


# ============================================================================
# Интеграционный тест: _from_browser с живым поиском и остановкой по stable
# ============================================================================

@pytest.mark.asyncio
async def test_from_browser_live_search_fast_stop_integration(tmp_path, monkeypatch):
    """Интеграционный тест: _from_browser создаёт задание fetch_jobs,

    evaluate парсит офферы, останавливает по 'stable', помечает partial=True,
    а record_attempt сохраняет stop_reason, t_first_offer, t_last_improve.
    """
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig(
        name="test_live_site",
        url_template="https://site.test/search",
        phone="dev1",
        parser="p_test_live",
        phone_wait_seconds=40,
    )
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="tok123",
        fast_stop=True,
        stop_stable_seconds=4.0,  # короткая пауза стабильности для теста
    )
    q = Query("MOW", "LED", date(2026, 12, 10))

    # Парсер сайта
    def p_live(snap: Snapshot, query: Query):
        results = []
        for url, body in snap.json_payloads:
            try:
                data = json.loads(body)
                for item in data.get("tickets", []):
                    results.append(
                        Offer(
                            site="test_live_site",
                            price=item["price"],
                            airline="SU",
                            depart_at=f"{query.depart.isoformat()}T10:00:00",
                        )
                    )
            except Exception:
                pass
        return results

    monkeypatch.setitem(PARSERS, "p_test_live", p_live)

    # Эмулируем хаб: 1-я порция (цена 8000), 2-я порция (цена 6000), 3-й GET (прошло 4.5 с -> stable)
    get_count = 0

    def hub_handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        url_str = str(request.url)

        if request.method == "POST" and "/fetch_jobs" in url_str and not url_str.endswith("/finish"):
            return httpx.Response(200, json={"job_id": "job_live_1"})

        if request.method == "GET" and "/fetch_jobs/job_live_1" in url_str:
            get_count += 1
            if get_count == 1:
                # 1-я порция: t=1.0, билет за 8000
                return httpx.Response(
                    200,
                    json={
                        "responses": [
                            {
                                "i": 0,
                                "t": 1.0,
                                "url": "https://site/api1",
                                "body": json.dumps({"tickets": [{"price": 8000}]}),
                            }
                        ],
                        "next": 1,
                        "state": "running",
                        "wait_for_seen": False,
                        "network_quiet": False,
                        "elapsed": 1.2,
                        "captcha": False,
                        "error": None,
                    },
                )
            elif get_count == 2:
                # 2-я порция: t=2.0, билет за 6000 (улучшение минимума)
                return httpx.Response(
                    200,
                    json={
                        "responses": [
                            {
                                "i": 1,
                                "t": 2.0,
                                "url": "https://site/api2",
                                "body": json.dumps({"tickets": [{"price": 6000}]}),
                            }
                        ],
                        "next": 2,
                        "state": "running",
                        "wait_for_seen": False,
                        "network_quiet": False,
                        "elapsed": 2.2,
                        "captcha": False,
                        "error": None,
                    },
                )
            else:
                # 3-й опрос: elapsed = 6.5 (6.5 - 2.0 = 4.5 >= 4.0 stable_seconds)
                return httpx.Response(
                    200,
                    json={
                        "responses": [],
                        "next": 2,
                        "state": "running",
                        "wait_for_seen": False,
                        "network_quiet": False,
                        "elapsed": 6.5,
                        "captcha": False,
                        "error": None,
                    },
                )

        if request.method == "POST" and url_str.endswith("/finish"):
            return httpx.Response(
                200,
                json={
                    "url": "https://site.test/search",
                    "text": "Страница найдена",
                    "captcha": False,
                    "finished": True,
                    "seconds": 6.5,
                    "json_responses": [],
                },
            )

        return httpx.Response(404)

    _mock_client(monkeypatch, hub_handler)

    # Живой поиск (priority=0)
    stats: dict = {}
    offers, page_url, partial = await _from_browser(
        site,
        q,
        settings,
        priority=0,
        stats=stats,
        search_id="search_test_1",
    )

    # Проверяем офферы и partial
    assert len(offers) == 2
    assert offers[0].price == 6000
    assert offers[1].price == 8000
    assert partial is True  # При 'stable' выдача считается неполной

    # Проверяем stats
    assert stats.get("stop_reason") == "stable"
    assert stats.get("t_first_offer") == 1.0
    assert stats.get("t_last_improve") == 2.0

    # Проверяем запись в attempts
    assert wait_metrics_io()  # попытки пишутся без ожидания: дождаться записи
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM attempts WHERE search_id = 'search_test_1'").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["outcome"] == "partial"
        assert r["stop_reason"] == "stable"
        assert r["t_first_offer"] == 1.0
        assert r["t_last_improve"] == 2.0
