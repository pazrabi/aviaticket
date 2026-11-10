"""Тесты механизма replay_post для глубокого поиска и слияния выдачи Туту."""
from __future__ import annotations

import asyncio
from datetime import date
import json
from pathlib import Path
from typing import Any
import httpx
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.droidhub import (
    _fetch_device_job,
    _fetch_device_snapshot,
    _reset_pool,
    fetch_page_snapshot,
    fetch_page_snapshot_any,
    reset_jobs_supported,
)
import avia.droidhub as droidhub
from avia.models import Query
from avia.parsers.tutu import merge_tutu_chunks, merge_tutu_payloads, parse_tutu
import avia.pipeline as pipeline

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tutu"
API_URL = "https://offers-api.tutu.ru/avia/offers/v2"
_ORIG_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _reset_hub_state():
    _reset_pool()
    reset_jobs_supported()
    orig_poll = droidhub.POLL_MIN_INTERVAL
    orig_del = droidhub.DELETE_TIMEOUT
    orig_parser = pipeline.PARSER_MIN_INTERVAL
    droidhub.POLL_MIN_INTERVAL = 0.0
    droidhub.DELETE_TIMEOUT = 1.0
    pipeline.PARSER_MIN_INTERVAL = 0.0
    yield
    _reset_pool()
    reset_jobs_supported()
    droidhub.POLL_MIN_INTERVAL = orig_poll
    droidhub.DELETE_TIMEOUT = orig_del
    pipeline.PARSER_MIN_INTERVAL = orig_parser


def _mock_transport(monkeypatch, handler):
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
# 1. replay_post попадает в тело fetch_page и fetch_jobs ТОЛЬКО при deep
# ============================================================================


@pytest.mark.asyncio
async def test_replay_post_in_fetch_page_only_on_deep(monkeypatch):
    """(1) replay_post передаётся в тело POST fetch_page только при deep=True."""
    replay_cfg = {
        "url_contains": "/avia/offers/v2",
        "set": {"sorting": "cheapest", "limit": 100, "offset": 0, "isNewSearch": False},
    }
    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru/{origin}-{destination}",
        source="browser",
        phone="dev1",
        enabled=True,
        phone_capture_json=["/avia/offers/v2"],
        replay_post=replay_cfg,
    )
    settings = Settings(
        sites=[site],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        droidhub_devices=["dev1"],
    )

    recorded_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/api/devices/dev1/fetch_page" in url_str:
            data = json.loads(request.content.decode("utf-8"))
            recorded_bodies.append(data)
            return httpx.Response(
                200,
                json={"url": str(request.url), "text": "OK", "finished": True, "json_responses": []},
            )
        return httpx.Response(404)

    _mock_transport(monkeypatch, handler)
    monkeypatch.setattr("avia.pipeline.phone_enabled", lambda s, sc: True)
    monkeypatch.setattr("avia.pipeline.candidates", lambda s, sc, priority=0: ["dev1"])

    q = Query("MOW", "LED", date(2026, 12, 10))

    # (а) Быстрый поиск (deep=False) — replay_post отсутствует
    recorded_bodies.clear()
    await pipeline.search(q, settings, deep=False)
    assert len(recorded_bodies) == 1
    assert "replay_post" not in recorded_bodies[0]

    # (б) Глубокий поиск (deep=True) — replay_post присутствует
    recorded_bodies.clear()
    await pipeline.search(q, settings, deep=True)
    assert len(recorded_bodies) == 1
    assert "replay_post" in recorded_bodies[0]
    assert recorded_bodies[0]["replay_post"] == replay_cfg


@pytest.mark.asyncio
async def test_replay_post_in_fetch_jobs_only_when_provided(monkeypatch):
    """(1б) replay_post попадает в тело POST fetch_jobs только при наличии."""
    replay_cfg = {"url_contains": "/avia/offers/v2", "set": {"limit": 100}}
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        droidhub_devices=["dev1"],
    )

    recorded_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/api/devices/dev1/fetch_jobs" in url_str:
            data = json.loads(request.content.decode("utf-8"))
            recorded_bodies.append(data)
            return httpx.Response(200, json={"job_id": "job_1"})
        if request.method == "GET" and "/api/fetch_jobs/job_1" in url_str:
            return httpx.Response(200, json={"state": "done", "responses": [], "next": 0})
        if request.method == "POST" and "/api/fetch_jobs/job_1/finish" in url_str:
            return httpx.Response(200, json={"url": "https://site", "text": "", "finished": True})
        return httpx.Response(404)

    _mock_transport(monkeypatch, handler)

    # Без replay_post
    recorded_bodies.clear()
    await _fetch_device_job(settings, "dev1", "https://site", replay_post=None)
    assert len(recorded_bodies) == 1
    assert "replay_post" not in recorded_bodies[0]

    # С replay_post
    recorded_bodies.clear()
    await _fetch_device_job(settings, "dev1", "https://site", replay_post=replay_cfg)
    assert len(recorded_bodies) == 1
    assert "replay_post" in recorded_bodies[0]
    assert recorded_bodies[0]["replay_post"] == replay_cfg


# ============================================================================
# 2. Откат при 400/422 от старой версии хаба
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 422])
async def test_fetch_device_snapshot_fallback_on_error_code(monkeypatch, status_code):
    """(2а) При ответе 400/422 на запрос с replay_post запрос повторяется без него."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )
    replay_cfg = {"url_contains": "/offers", "set": {"limit": 100}}

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content.decode("utf-8"))
        calls.append(data)
        if "replay_post" in data:
            return httpx.Response(status_code, json={"detail": "unknown field replay_post"})
        return httpx.Response(
            200,
            json={"url": "https://ok.site", "text": "fallback success", "finished": True, "json_responses": []},
        )

    _mock_transport(monkeypatch, handler)

    snap, fin = await _fetch_device_snapshot(
        settings,
        "dev1",
        "https://ok.site",
        replay_post=replay_cfg,
    )

    assert len(calls) == 2
    assert "replay_post" in calls[0]
    assert "replay_post" not in calls[1]
    assert snap.text == "fallback success"
    assert fin is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 422])
async def test_fetch_device_job_fallback_on_error_code(monkeypatch, status_code):
    """(2б) При ответе 400/422 на POST fetch_jobs с replay_post запрос повторяется без него."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )
    replay_cfg = {"url_contains": "/offers", "set": {"limit": 100}}

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/api/devices/dev1/fetch_jobs" in url_str:
            data = json.loads(request.content.decode("utf-8"))
            calls.append(data)
            if "replay_post" in data:
                return httpx.Response(status_code, json={"detail": "unknown field replay_post"})
            return httpx.Response(200, json={"job_id": "job_retry"})
        if request.method == "GET" and "/api/fetch_jobs/job_retry" in url_str:
            return httpx.Response(200, json={"state": "done", "responses": [], "next": 0})
        if request.method == "POST" and "/api/fetch_jobs/job_retry/finish" in url_str:
            return httpx.Response(200, json={"url": "https://ok.site", "text": "job success", "finished": True})
        return httpx.Response(404)

    _mock_transport(monkeypatch, handler)

    snap, fin, info = await _fetch_device_job(
        settings,
        "dev1",
        "https://ok.site",
        replay_post=replay_cfg,
    )

    assert len(calls) == 2
    assert "replay_post" in calls[0]
    assert "replay_post" not in calls[1]
    assert snap.text == "job success"
    assert fin is True


# ============================================================================
# 3. Сохранение replayed: true из ответов хаба в Snapshot.json_payloads
# ============================================================================


@pytest.mark.asyncio
async def test_replayed_element_preserved_in_snapshot_payloads(monkeypatch):
    """(3) Элемент {url, body, t, replayed: true} сохраняет флаг replayed в Snapshot.json_payloads."""
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )

    chunk1 = json.dumps([{"summary": {"offersCurrentAmount": 10}, "offers": {"actual": {}}}])
    chunk2 = json.dumps([{"summary": {"offersCurrentAmount": 20}, "offers": {"actual": {}}}])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "url": "https://tutu.ru",
                "text": "",
                "finished": True,
                "json_responses": [
                    {"url": API_URL, "body": chunk1, "t": 1.0},
                    {"url": API_URL, "body": chunk2, "t": 2.0, "replayed": True},
                ],
            },
        )

    _mock_transport(monkeypatch, handler)

    snap, fin = await _fetch_device_snapshot(settings, "dev1", "https://tutu.ru")
    assert len(snap.json_payloads) == 2

    # Первый ответ — обычный
    p1 = json.loads(snap.json_payloads[0][1])[0]
    assert p1.get("replayed") is not True

    # Второй ответ — replayed=True сохранён в теле
    p2 = json.loads(snap.json_payloads[1][1])[0]
    assert p2.get("replayed") is True


# ============================================================================
# 4. Парсер и слияние Туту: не дублирует офферы при двух payload
# ============================================================================


def test_tutu_merge_chunks_deduplication():
    """Слияние чанков Туту объединяет справочники и дедуплицирует офферы по id."""
    chunk1 = {
        "summary": {"offersCurrentAmount": 2, "totalMinPrice": 5000},
        "dictionary": {
            "common": {
                "routes": {"r1": {"id": "r1", "segmentIds": ["s1"]}},
                "segments": {"s1": {"id": "s1"}},
            },
            "avia": {"conditions": {}},
        },
        "offers": {
            "actual": {
                "off_1": {"id": "off_1", "routeIds": ["r1"]},
                "off_common": {"id": "off_common", "routeIds": ["r1"]},
            }
        },
    }

    chunk2 = {
        "summary": {"offersCurrentAmount": 2, "totalMinPrice": 4500},
        "dictionary": {
            "common": {
                "routes": {"r2": {"id": "r2", "segmentIds": ["s2"]}},
                "segments": {"s2": {"id": "s2"}},
            },
            "avia": {"conditions": {}},
        },
        "offers": {
            "actual": {
                "off_common": {"id": "off_common", "routeIds": ["r1"]},  # дубликат по id
                "off_2": {"id": "off_2", "routeIds": ["r2"]},
            }
        },
    }

    merged = merge_tutu_chunks(chunk1, chunk2)

    # Ровно 3 оффера (off_1, off_common, off_2) без удвоения off_common
    actual = merged["offers"]["actual"]
    assert len(actual) == 3
    assert set(actual.keys()) == {"off_1", "off_common", "off_2"}

    # Справочники объединены
    routes = merged["dictionary"]["common"]["routes"]
    assert "r1" in routes and "r2" in routes

    # merge_tutu_payloads работает идентично со строками JSON
    payload_merged = merge_tutu_payloads(json.dumps([chunk1]), json.dumps([chunk2]))
    assert len(payload_merged["offers"]["actual"]) == 3


def test_tutu_parser_merges_replayed_payload_without_duplication():
    """Парсер Туту разбирает snapshot с replayed payload и объединяет первую и вторую страницу без дублирования."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    fixture_text = (FIXTURES_DIR / "offers_first_mow_ala.json").read_text(encoding="utf-8")
    data_page1 = json.loads(fixture_text)

    # Создаём вторую порцию (replay_post) со второй страницей
    data_page2 = json.loads(fixture_text)
    # Помечаем как replayed
    data_page2[0]["replayed"] = True

    # Модифицируем page2: оставляем 1 общий оффер (дубликат по id) и добавляем 1 новый синтетический оффер
    actual_p1 = data_page1[0]["offers"]["actual"]
    first_key = list(actual_p1.keys())[0]

    # Создаём новый уникальный оффер в page2
    new_offer = json.loads(json.dumps(actual_p1[first_key]))
    new_id = "synthetic_new_offer_page2_id"
    new_offer["id"] = new_id
    # Меняем цену тарифного варианта
    new_offer["offerVariants"][0]["price"]["value"]["amount"] = 1500000  # 15000 руб
    # Меняем маршрут на новый уникальный
    new_route_id = "route_synth_2"
    new_offer["routeIds"] = [new_route_id]

    data_page2[0]["offers"]["actual"] = {
        first_key: actual_p1[first_key],  # Дубликат оффера с первой страницы
        new_id: new_offer,                # Новый оффер со второй страницы
    }
    data_page2[0]["dictionary"]["common"]["routes"][new_route_id] = {
        "id": new_route_id,
        "segmentIds": data_page1[0]["dictionary"]["common"]["routes"][actual_p1[first_key]["routeIds"][0]]["segmentIds"],
        "transferIds": [],
    }

    snap_single = Snapshot(
        url="https://avia.tutu.ru",
        text="",
        json_payloads=[(API_URL, json.dumps(data_page1))],
    )
    offers_single = parse_tutu(snap_single, q)
    assert len(offers_single) == 4

    snap_with_replay = Snapshot(
        url="https://avia.tutu.ru",
        text="",
        json_payloads=[
            (API_URL, json.dumps(data_page1)),
            (API_URL, json.dumps(data_page2)),
        ],
    )
    offers_merged = parse_tutu(snap_with_replay, q)

    # Должно быть ровно 5 офферов (4 исходных + 1 новый), дубликат не удвоился
    assert len(offers_merged) == 5
    prices = [o.price for o in offers_merged]
    assert 15000 in prices  # Новый оффер со второй страницы присутствует
    assert prices == sorted(prices)
