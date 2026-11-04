from __future__ import annotations

from datetime import date
import json

import httpx
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.droidhub import DroidhubError
from avia.guard import SearchGuard
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import _from_browser, search


@pytest.fixture(autouse=True)
def clean_pipeline_guard():
    from avia.pipeline import guard as default_guard

    default_guard.clear()
    orig_interval = default_guard.min_interval
    orig_retry = default_guard.retry_delay
    default_guard.min_interval = 0.0
    default_guard.retry_delay = 0.0
    yield
    default_guard.clear()
    default_guard.min_interval = orig_interval
    default_guard.retry_delay = orig_retry


@pytest.mark.asyncio
async def test_pipeline_phone_captcha(monkeypatch):
    """captcha=true на телефоне даёт ошибку RuntimeError в _from_browser и SiteResult(ok=False) в поиске."""
    async def fake_fetch_page_snapshot_any(settings, candidates, url, **kwargs):
        return Snapshot(url=url, text="Капча", blocked=True), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)

    site = SiteConfig("aviasales", "https://aviasales.ru/{origin}{destination}", phone="realme", parser="aviasales")
    settings = Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="secret_token")
    q = Query("MOW", "ALA", date(2026, 12, 10))

    # Прямой вызов _from_browser бросает RuntimeError("сайт показал капчу на телефоне")
    with pytest.raises(RuntimeError, match="сайт показал капчу на телефоне"):
        await _from_browser(site, q, settings)

    # В общем пайплайне search() сайт завершается с ok=False и дружественным текстом ошибки
    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is False
    assert "капч" in results[0].error.lower()
    assert results[0].page_url is not None


@pytest.mark.asyncio
async def test_pipeline_phone_finished_false_partial(monkeypatch):
    """finished=false даёт partial=True в SiteResult."""
    def dummy_parser(snap, q):
        return [Offer(site="aviasales", price=9500, airline="Airline")]

    # checker возвращает True, но так как finished=False, partial должен остаться True
    dummy_parser.results_complete = lambda snap: True  # type: ignore[attr-defined]
    monkeypatch.setitem(PARSERS, "phone_test_parser", dummy_parser)

    async def fake_fetch_page_snapshot_any(settings, candidates, url, **kwargs):
        return Snapshot(
            url="https://aviasales.ru/search/MOW1012ALA1",
            text="Идёт поиск билетов...",
            json_payloads=[("/search/v3.2/results", '{"tickets": [{"price": 9500}]}')],
            blocked=False,
        ), False  # finished=False

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)

    site = SiteConfig(
        "aviasales",
        "https://aviasales.ru/search/{origin}{ddmm}{destination}{adults}",
        phone="realme",
        parser="phone_test_parser",
    )
    settings = Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="secret_token")
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    res = results[0]
    assert res.ok is True
    assert res.partial is True
    assert len(res.offers) == 1
    assert res.offers[0].price == 9500


@pytest.mark.asyncio
async def test_pipeline_phone_droidhub_error_no_chromium_fallback(monkeypatch):
    """DroidhubError превращается в SiteResult(ok=False) со ссылкой на выдачу, без отката на Chromium."""
    chromium_called = False

    async def fake_chromium_fetch(*args, **kwargs):
        nonlocal chromium_called
        chromium_called = True
        return Snapshot(url="https://site.test", text="chromium")

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_chromium_fetch)

    async def fake_fetch_page_snapshot_any(*args, **kwargs):
        raise DroidhubError("телефон недоступен или страница не открылась (502)")

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)

    site = SiteConfig(
        "aviasales",
        "https://www.aviasales.ru/search/{origin}{ddmm}{destination}{adults}",
        phone="realme",
        parser="aviasales",
    )
    settings = Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="super_secret_token")
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    res = results[0]

    assert res.ok is False
    assert res.error == "телефон недоступен или страница не открылась (502)"
    assert res.page_url is not None
    assert "https://www.aviasales.ru/search/" in res.page_url
    # На Chromium отката быть не должно!
    assert chromium_called is False
    assert "super_secret_token" not in res.error


@pytest.mark.asyncio
async def test_pipeline_site_without_phone_uses_chromium(monkeypatch):
    """Сайт без phone (или без токена droidhub) идёт обычным путём Chromium."""
    chromium_called = False
    phone_called = False

    async def fake_chromium_fetch(url, site, **kwargs):
        nonlocal chromium_called
        chromium_called = True
        return Snapshot(url=url, text="chromium page text", blocked=False)

    async def fake_phone_fetch(*args, **kwargs):
        nonlocal phone_called
        phone_called = True
        return Snapshot(url="phone", text="phone"), True

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_chromium_fetch)
    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_phone_fetch)

    def dummy_parser(snap, q):
        return [Offer(site=snap.url, price=5000)]

    monkeypatch.setitem(PARSERS, "chrom_parser", dummy_parser)

    # 1. Сайт без phone при наличии токена droidhub
    site_no_phone = SiteConfig("site_no_phone", "https://site1.test/{origin}", phone="", parser="chrom_parser")
    settings = Settings(sites=[site_no_phone], droidhub_url="https://hub.test", droidhub_token="token")
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is True
    assert chromium_called is True
    assert phone_called is False

    # 2. Сайт с phone, но без токена droidhub -> тоже идёт через Chromium
    chromium_called = False
    phone_called = False
    site_with_phone = SiteConfig("site_with_phone", "https://site2.test/{origin}", phone="realme", parser="chrom_parser")
    settings_no_token = Settings(sites=[site_with_phone], droidhub_url="https://hub.test", droidhub_token="")

    results2 = await search(q, settings_no_token, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results2) == 1
    assert results2[0].ok is True
    assert chromium_called is True
    assert phone_called is False

    # 3. Сайт с phone="auto", но без списка droidhub_devices -> тоже идёт через Chromium
    chromium_called = False
    phone_called = False
    site_auto_empty = SiteConfig("site_auto", "https://site3.test/{origin}", phone="auto", parser="chrom_parser")
    settings_auto_empty = Settings(sites=[site_auto_empty], droidhub_url="https://hub.test", droidhub_token="tok", droidhub_devices=[])

    results3 = await search(q, settings_auto_empty, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results3) == 1
    assert results3[0].ok is True
    assert chromium_called is True
    assert phone_called is False


@pytest.mark.asyncio
async def test_pipeline_phone_auto_with_devices(monkeypatch):
    """Сайт с phone='auto' при заданном пуле телефонов вызывает fetch_page_snapshot_any с кандидатами."""
    received_candidates = []

    async def fake_fetch_any(settings, candidates, url, **kwargs):
        received_candidates.extend(candidates)
        return Snapshot(
            url=url,
            text="Пул телефонов выдал билеты",
            json_payloads=[("/search/results", '{"tickets": [{"price": 7700}]}')],
            blocked=False,
        ), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)

    def dummy_parser(snap, q):
        return [Offer(site="pool_site", price=7700)]

    monkeypatch.setitem(PARSERS, "pool_parser", dummy_parser)

    site = SiteConfig("pool_site", "https://pool.test/{origin}{destination}", phone="auto", parser="pool_parser")
    settings = Settings(
        sites=[site],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        droidhub_devices=["phone_a", "phone_b"],
    )
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is True
    assert len(results[0].offers) == 1
    assert results[0].offers[0].price == 7700
    assert received_candidates == ["phone_a", "phone_b"]


@pytest.mark.asyncio
async def test_pipeline_passes_priority_weight_and_receives_stats(monkeypatch):
    """Pipeline search передаёт priority и weight в _from_browser и SiteResult получает stats."""
    received_kwargs = {}

    async def fake_fetch_any(settings, candidates, url, **kwargs):
        received_kwargs.update(kwargs)
        st = kwargs.get("stats")
        if st is not None:
            st["device"] = "phone_a"
            st["queue_wait"] = 0.35
            st["fetch_seconds"] = 1.2
            st["attempts"] = 1
        return Snapshot(
            url=url,
            text="Билеты найдены",
            json_payloads=[("/search/results", '{"tickets": [{"price": 5000}]}')],
            blocked=False,
        ), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)

    def dummy_parser(snap, q):
        return [Offer(site="stat_site", price=5000)]

    monkeypatch.setitem(PARSERS, "stat_parser", dummy_parser)

    site = SiteConfig(
        "stat_site",
        "https://stat.test/{origin}{destination}",
        phone="phone_a",
        parser="stat_parser",
        phone_wait_seconds=45,
    )
    settings = Settings(
        sites=[site],
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, priority=1, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    res = results[0]
    assert res.ok is True
    assert res.device == "phone_a"
    assert res.queue_wait == 0.35
    assert res.fetch_seconds == 1.2
    assert received_kwargs.get("priority") == 1
    assert received_kwargs.get("weight") == 45

