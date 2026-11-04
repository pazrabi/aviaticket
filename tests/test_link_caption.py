from __future__ import annotations

from datetime import date
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.formatting import format_details
from avia.models import Leg, Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import _from_browser


def test_format_details_direct_link():
    """url_is_direct=True даёт подпись 'купить билет' и не добавляет строку с поиском в выдаче."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="tutu",
        price=18000,
        depart_time="10:00",
        flight_numbers=["SU 100"],
        url="https://avia.tutu.ru/offers/?pin=123",
        url_is_direct=True,
    )
    card = format_details(o, q)

    assert '<a href="https://avia.tutu.ru/offers/?pin=123">купить билет</a>' in card
    assert "открыть выдачу сайта" not in card
    assert "Найдите рейс в выдаче" not in card


def test_format_details_non_direct_link_both_time_and_flight_numbers():
    """url_is_direct=False с известными временем и номером рейса."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="yandex_travel",
        price=19000,
        depart_time="10:00",
        flight_numbers=["SU 100"],
        url="https://travel.yandex.ru/avia/search/result/?...",
        url_is_direct=False,
    )
    card = format_details(o, q)

    assert "открыть выдачу сайта" in card
    assert "купить билет" not in card
    assert "Найдите рейс в выдаче по времени вылета и номеру рейса" in card


def test_format_details_non_direct_link_only_time():
    """url_is_direct=False только с известным временем вылета (как в Aviasales)."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="aviasales",
        price=15000,
        depart_time="14:30",
        flight_numbers=None,
        url="https://www.aviasales.ru/search/MOW1012ALA1",
        url_is_direct=False,
    )
    card = format_details(o, q)

    assert '<a href="https://www.aviasales.ru/search/MOW1012ALA1">открыть выдачу сайта</a>' in card
    assert "Найдите рейс в выдаче по времени вылета" in card
    assert "номеру рейса" not in card


def test_format_details_non_direct_link_only_flight_number():
    """url_is_direct=False только с известным номером рейса."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="custom_site",
        price=15000,
        depart_time=None,
        flight_numbers=["KC 101"],
        url="https://custom.site/search",
        url_is_direct=False,
    )
    card = format_details(o, q)

    assert "Найдите рейс в выдаче по номеру рейса" in card
    assert "времени вылета" not in card


def test_format_details_non_direct_link_neither():
    """url_is_direct=False без времени и номера рейса — строка поиска не добавляется."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="custom_site",
        price=15000,
        depart_time=None,
        flight_numbers=None,
        url="https://custom.site/search",
        url_is_direct=False,
    )
    card = format_details(o, q)

    assert '<a href="https://custom.site/search">открыть выдачу сайта</a>' in card
    assert "Найдите рейс в выдаче" not in card


def test_format_details_roundtrip_single_link_and_caption():
    """Туда-обратно: ссылка одна, подпись одна, данные берутся с обоих плеч."""
    q = Query("MOW", "ALA", date(2026, 12, 10), return_date=date(2026, 12, 20))
    back = Leg(airline="Air Astana", depart_time="18:00", flight_numbers=["KC 102"])
    o = Offer(
        site="aviasales",
        price=30000,
        depart_time="10:00",
        flight_numbers=None,  # в прямом плече нет рейса, но есть в обратном
        back=back,
        url="https://www.aviasales.ru/search/MOW1012ALA20121",
        url_is_direct=False,
    )
    card = format_details(o, q)

    assert card.count("открыть выдачу сайта") == 1
    assert "открыть выдачу сайта" in card
    assert "купить билет" not in card
    assert "Найдите рейс в выдаче по времени вылета и номеру рейса" in card


def test_format_details_escaping():
    """URL и данные корректно экранируются в HTML."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="safe_site",
        price=10000,
        depart_time="10:00",
        url="https://site.test/search?a=1&b=2<script>",
        url_is_direct=False,
    )
    card = format_details(o, q)

    assert '<a href="https://site.test/search?a=1&amp;b=2&lt;script&gt;">открыть выдачу сайта</a>' in card
    assert "<script>" not in card


@pytest.mark.asyncio
async def test_pipeline_from_browser_sets_url_is_direct(monkeypatch):
    """pipeline._from_browser корректно выставляет url_is_direct."""
    snap = Snapshot(
        url="https://site.test/search_page",
        text="page content",
        json_payloads=[],
    )

    async def fake_fetch_snapshot(url, site, **kwargs):
        return snap

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    # 1. Парсер вернул свой URL (отличный от snap.url) -> url_is_direct = True
    def direct_parser(s, q):
        return [Offer("site1", 10000, url="https://site.test/pin/123")]

    monkeypatch.setitem(PARSERS, "direct_parser", direct_parser)
    site_direct = SiteConfig("site1", "https://site.test/search", parser="direct_parser")
    settings = Settings(sites=[site_direct])
    q = Query("MOW", "ALA", date(2026, 12, 10))

    offers, page_url, _ = await _from_browser(site_direct, q, settings)
    assert len(offers) == 1
    assert offers[0].url == "https://site.test/pin/123"
    assert offers[0].url_is_direct is True

    # 2. Парсер не заполнил URL -> url = snap.url, url_is_direct = False
    def no_url_parser(s, q):
        return [Offer("site2", 10000, url=None)]

    monkeypatch.setitem(PARSERS, "no_url_parser", no_url_parser)
    site_no_url = SiteConfig("site2", "https://site.test/search", parser="no_url_parser")

    offers2, page_url2, _ = await _from_browser(site_no_url, q, settings)
    assert len(offers2) == 1
    assert offers2[0].url == snap.url
    assert offers2[0].url_is_direct is False

    # 3. Парсер заполнил URL равный snap.url -> url_is_direct = False
    def same_url_parser(s, q):
        return [Offer("site3", 10000, url=snap.url)]

    monkeypatch.setitem(PARSERS, "same_url_parser", same_url_parser)
    site_same_url = SiteConfig("site3", "https://site.test/search", parser="same_url_parser")

    offers3, page_url3, _ = await _from_browser(site_same_url, q, settings)
    assert len(offers3) == 1
    assert offers3[0].url == snap.url
    assert offers3[0].url_is_direct is False
