"""Повтор вызова _from_browser в _run_site — только при несовпадении сигнатуры.

Ошибка TypeError внутри парсера — настоящая ошибка: страница собирается один раз,
а не два-три (раньше любой TypeError принимался за старую сигнатуру и запускал сбор заново).
"""
from __future__ import annotations

from datetime import date
import json

import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.guard import SearchGuard
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import search

NULL_PRICE_PAYLOAD = ("/search/results", '{"tickets": [{"price": null}]}')
_MISSING = object()


@pytest.fixture
def query() -> Query:
    return Query("MOW", "ALA", date(2026, 12, 10))


@pytest.fixture
def test_guard() -> SearchGuard:
    return SearchGuard(min_interval=0.0, retry_delay=0.0)


def _broken_parser(snap, q):
    """Парсер с настоящей ошибкой: сайт отдал price: null, а код делает int(None)."""
    data = json.loads(snap.json_payloads[0][1])
    return [Offer(site="x", price=int(t["price"])) for t in data["tickets"]]


@pytest.mark.asyncio
async def test_parser_type_error_collects_page_once_in_browser(monkeypatch, query, test_guard):
    """Chromium: TypeError в парсере → 1 сбор страницы, сайт завершается с ошибкой."""
    fetches = 0

    async def fake_fetch_snapshot(url, site, **kwargs):
        nonlocal fetches
        fetches += 1
        return Snapshot(url=url, text="Билеты найдены", json_payloads=[NULL_PRICE_PAYLOAD])

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)
    monkeypatch.setitem(PARSERS, "typeerror_parser", _broken_parser)

    site = SiteConfig("typeerror_chrome", "https://chrome.test/{origin}{destination}", parser="typeerror_parser")
    settings = Settings(sites=[site])

    results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    assert results[0].ok is False
    assert fetches == 1


@pytest.mark.asyncio
async def test_parser_type_error_collects_page_once_on_phone(monkeypatch, query, test_guard):
    """Телефон: TypeError в парсере → 1 сбор страницы на телефоне, а не 3."""
    fetches = 0

    async def fake_fetch_page_snapshot_any(settings, candidates, url, **kwargs):
        nonlocal fetches
        fetches += 1
        st = kwargs.get("stats")
        if st is not None:
            st["device"] = "phone_a"
        return Snapshot(url=url, text="Билеты найдены", json_payloads=[NULL_PRICE_PAYLOAD]), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)
    monkeypatch.setitem(PARSERS, "typeerror_parser", _broken_parser)

    site = SiteConfig(
        "typeerror_phone",
        "https://phone.test/{origin}{destination}",
        phone="phone_a",
        parser="typeerror_parser",
    )
    settings = Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="test_token")

    results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    assert results[0].ok is False
    assert fetches == 1


@pytest.mark.asyncio
async def test_old_signature_without_kwargs_still_works(monkeypatch, query, test_guard):
    """Совместимость: _from_browser(site, q, settings) без новых аргументов вызывается повторно без них."""
    runs = 0

    async def old_from_browser(site, q, settings):
        nonlocal runs
        runs += 1
        return [Offer(site=site.name, price=4200, airline="OldAir")], "https://old.test/result", False

    monkeypatch.setattr("avia.pipeline._from_browser", old_from_browser)

    site = SiteConfig("old_sig", "https://old.test/{origin}{destination}", parser="any_parser")
    results = await search(query, Settings(sites=[site]), guard=test_guard)

    assert len(results) == 1
    assert results[0].ok is True
    assert [o.price for o in results[0].offers] == [4200]
    assert runs == 1


@pytest.mark.asyncio
async def test_middle_signature_gets_only_known_kwargs(monkeypatch, query, test_guard):
    """Совместимость: сигнатура с priority/weight/stats получает повтор ровно с ними."""
    received: list[set[str]] = []

    async def mid_from_browser(site, q, settings, *, priority=_MISSING, weight=_MISSING, stats=_MISSING):
        passed = {"priority": priority, "weight": weight, "stats": stats}
        received.append({name for name, value in passed.items() if value is not _MISSING})
        return [Offer(site=site.name, price=3100, airline="MidAir")], "https://mid.test/result", False

    monkeypatch.setattr("avia.pipeline._from_browser", mid_from_browser)

    site = SiteConfig("mid_sig", "https://mid.test/{origin}{destination}", parser="any_parser")
    results = await search(query, Settings(sites=[site]), guard=test_guard)

    assert len(results) == 1
    assert results[0].ok is True
    assert [o.price for o in results[0].offers] == [3100]
    assert received == [{"priority", "weight", "stats"}]


@pytest.mark.asyncio
async def test_type_error_after_signature_fallback_is_not_retried(monkeypatch, query, test_guard):
    """Повтор со старой сигнатурой собрал страницу и упал в парсере → третьего сбора нет."""
    runs = 0

    async def mid_from_browser(site, q, settings, *, priority=0, weight=0, stats=None):
        nonlocal runs
        runs += 1
        return _broken_parser(Snapshot(url="https://mid.test/", text="", json_payloads=[NULL_PRICE_PAYLOAD]), q)

    monkeypatch.setattr("avia.pipeline._from_browser", mid_from_browser)

    site = SiteConfig("mid_sig_broken", "https://mid.test/{origin}{destination}", parser="any_parser")
    results = await search(query, Settings(sites=[site]), guard=test_guard)

    assert len(results) == 1
    assert results[0].ok is False
    assert runs == 1
