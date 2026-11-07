"""Тесты формирования ссылки на конкретный билет Купибилет."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.formatting import format_details, offer_card, offer_link_caption, offer_link_html
from avia.models import Leg, Offer, Query
from avia.parsers.kupibilet import build_ticket_url, parse_kupibilet, ticket_hash
from avia.pipeline import _process_snapshot
from avia.scheduler import build_notification
from avia.subscriptions import Subscription

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "kupibilet"
SEARCH_URL_ENDPOINT = "https://api-rs-lb.kupibilet.ru/frontend_search"
KUPIBILET_PAGE_URL = (
    "https://www.kupibilet.ru/search?route%5B0%5D=iatax%3AMOW_2026-12-20_date_2026-12-20_iatax%3AEVN"
    "&adult=1&child=0&infant=0&childrenAges=%5B%5D&cabinClass=Y&v=2"
)


def _load_fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _make_snapshot(payload_text: str, page_url: str = KUPIBILET_PAGE_URL) -> Snapshot:
    return Snapshot(
        url=page_url,
        text="Купибилет поиск",
        json_payloads=[(SEARCH_URL_ENDPOINT, payload_text)],
    )


def _make_site() -> SiteConfig:
    return SiteConfig(
        name="kupibilet",
        url_template=KUPIBILET_PAGE_URL,
        parser="kupibilet",
    )


def test_owner_example():
    """Пример владельца: вылет 2026-12-20T21:00:00+03:00, 3F 3316 -> ticket 1797714000000-3F-3316."""
    variant = {
        "id": "var1",
        "segments": [{"flights": ["fl1"]}],
    }
    flights_map = {
        "fl1": {
            "departure_datetime": "2026-12-20T21:00:00+03:00",
            "marketing_carrier": "3F",
            "number": 3316,
        }
    }
    th = ticket_hash(variant, flights_map)
    assert th == "1797714000000-3F-3316"

    url = build_ticket_url(KUPIBILET_PAGE_URL, th)
    expected_url = (
        f"{KUPIBILET_PAGE_URL}&ticket=1797714000000-3F-3316"
        "&utm_source=direct&utm_medium=search&utm_campaign=tickets&agent=kup747&tag=searchru"
    )
    assert url == expected_url


def test_offset_plus_4():
    """Смещение +04:00 (вычитается 4 часа от UTC полуночи) и формат +0400 без двоеточия."""
    variant = {
        "id": "var1",
        "segments": [{"flights": ["fl1"]}],
    }
    # 2026-12-20 UTC midnight = 1797724800000 ms.
    # 1797724800000 - 4 * 3600 * 1000 = 1797710400000
    flights_map_colon = {
        "fl1": {
            "departure_datetime": "2026-12-20T21:00:00+04:00",
            "marketing_carrier": "3F",
            "number": "3316",
        }
    }
    assert ticket_hash(variant, flights_map_colon) == "1797710400000-3F-3316"

    flights_map_no_colon = {
        "fl1": {
            "departure_datetime": "2026-12-20T21:00:00+0400",
            "marketing_carrier": "3F",
            "transport_number": 3316,
        }
    }
    assert ticket_hash(variant, flights_map_no_colon) == "1797710400000-3F-3316"


def test_layover_ticket():
    """Билет с пересадкой: два рейса одного сегмента соединяются через '_'."""
    variant = {
        "id": "var_layover",
        "segments": [{"flights": ["fl1", "fl2"]}],
    }
    flights_map = {
        "fl1": {
            "departure_datetime": "2026-12-20T21:00:00+03:00",
            "marketing_carrier": "3F",
            "number": 3316,
        },
        "fl2": {
            # 2026-12-21 UTC midnight = 1797811200000; -4h (14400000) = 1797796800000
            "departure_datetime": "2026-12-21T06:00:00+04:00",
            "marketing_carrier": "SU",
            "number": 1234,
        },
    }
    th = ticket_hash(variant, flights_map)
    assert th == "1797714000000-3F-3316_1797796800000-SU-1234"


def test_roundtrip_ticket():
    """Туда-обратно: два направления (сегмента) соединяются через '_'."""
    variant = {
        "id": "var_rt",
        "segments": [{"flights": ["fl1"]}, {"flights": ["fl2"]}],
    }
    flights_map = {
        "fl1": {
            "departure_datetime": "2026-12-20T21:00:00+03:00",
            "marketing_carrier": "3F",
            "number": 3316,
        },
        "fl2": {
            # 2026-12-27 UTC midnight = 1798329600000; -3h (10800000) = 1798318800000
            "departure_datetime": "2026-12-27T10:00:00+03:00",
            "marketing_carrier": "SU",
            "number": 100,
        },
    }
    th = ticket_hash(variant, flights_map)
    assert th == "1797714000000-3F-3316_1798318800000-SU-100"


def test_missing_data_fallback_to_search_link():
    """При отсутствии даты/смещения/перевозчика/номера хеш не строится, ссылка остаётся на выдачу."""
    # Без смещения
    v_no_offset = {"segments": [{"flights": ["f1"]}]}
    assert ticket_hash(v_no_offset, {"f1": {"departure_datetime": "2026-12-20T21:00:00", "marketing_carrier": "3F", "number": 1}}) is None

    # Без даты
    assert ticket_hash(v_no_offset, {"f1": {"marketing_carrier": "3F", "number": 1}}) is None

    # Без перевозчика
    assert ticket_hash(v_no_offset, {"f1": {"departure_datetime": "2026-12-20T21:00:00+03:00", "number": 1}}) is None

    # Без номера
    assert ticket_hash(v_no_offset, {"f1": {"departure_datetime": "2026-12-20T21:00:00+03:00", "marketing_carrier": "3F"}}) is None

    # Несуществующий рейс
    assert ticket_hash(v_no_offset, {}) is None


@pytest.mark.asyncio
async def test_pipeline_missing_data_fallback():
    """В pipeline оффер без ticket_hash получает ссылку на выдачу и url_is_direct=False."""
    payload = {
        "variants": [
            {
                "id": "v1",
                "price": {"amount": "5000", "currency": "RUB"},
                "segments": [{"flights": ["f_nodata"]}],
            }
        ],
        "flights": {
            "f_nodata": {
                "departure_datetime": "2026-12-20T21:00:00",  # нет смещения
                "marketing_carrier": "3F",
                "number": 3316,
            }
        },
    }
    snap = _make_snapshot(json.dumps(payload))
    q = Query("MOW", "EVN", date(2026, 12, 20))
    site = _make_site()
    settings = Settings(sites=[site], kupibilet_ticket_link=True)

    offers, page_url, _ = await _process_snapshot(site, snap, q, settings)
    assert len(offers) == 1
    assert offers[0].ticket_hash is None
    assert offers[0].url == KUPIBILET_PAGE_URL
    assert offers[0].url_is_direct is False


@pytest.mark.asyncio
async def test_flag_zero_disables_ticket_link():
    """При KUPIBILET_TICKET_LINK=0 оффер получает ссылку на выдачу, а не на билет."""
    payload = {
        "variants": [
            {
                "id": "v1",
                "price": {"amount": "5000", "currency": "RUB"},
                "segments": [{"flights": ["f1"]}],
            }
        ],
        "flights": {
            "f1": {
                "departure_datetime": "2026-12-20T21:00:00+03:00",
                "marketing_carrier": "3F",
                "number": 3316,
            }
        },
    }
    snap = _make_snapshot(json.dumps(payload))
    q = Query("MOW", "EVN", date(2026, 12, 20))
    site = _make_site()
    settings = Settings(sites=[site], kupibilet_ticket_link=False)

    offers, page_url, _ = await _process_snapshot(site, snap, q, settings)
    assert len(offers) == 1
    assert offers[0].ticket_hash == "1797714000000-3F-3316"
    assert offers[0].url == KUPIBILET_PAGE_URL
    assert offers[0].url_is_direct is False


def test_final_url_contains_search_url_and_utm():
    """Итоговая ссылка содержит исходный URL поиска и все utm-параметры."""
    base_url = "https://www.kupibilet.ru/search?origin=MOW&destination=EVN"
    ticket = "1797714000000-3F-3316"
    url = build_ticket_url(base_url, ticket)

    assert url.startswith(base_url)
    assert "&ticket=1797714000000-3F-3316" in url
    assert "utm_source=direct" in url
    assert "utm_medium=search" in url
    assert "utm_campaign=tickets" in url
    assert "agent=kup747" in url
    assert "tag=searchru" in url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "query"),
    [
        ("search_quick_mow_ala.json", Query("MOW", "ALA", date(2026, 12, 12))),
        ("search_full_mow_ala.json", Query("MOW", "ALA", date(2026, 12, 12))),
        (
            "search_rt_kuf_evn.json",
            Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16)),
        ),
    ],
)
async def test_all_fixtures_have_nonempty_ticket(fixture_name: str, query: Query):
    """На всех фикстурах tests/fixtures/kupibilet/*.json все офферы получают непустой ticket."""
    text = _load_fixture_text(fixture_name)
    snap = _make_snapshot(text)
    offers = parse_kupibilet(snap, query)
    assert len(offers) > 0

    for o in offers:
        assert o.ticket_hash is not None
        assert len(o.ticket_hash) > 0
        assert "-" in o.ticket_hash

    # Проверяем, что через pipeline ссылка успешно формируется и становится прямой
    site = _make_site()
    settings = Settings(sites=[site], kupibilet_ticket_link=True)
    pipe_offers, _, _ = await _process_snapshot(site, snap, query, settings)
    assert len(pipe_offers) == len(offers)
    for o in pipe_offers:
        assert o.url is not None
        assert o.url.startswith(KUPIBILET_PAGE_URL)
        assert f"&ticket={o.ticket_hash}" in o.url
        assert "utm_source=direct" in o.url
        assert o.url_is_direct is True


def test_card_and_notification_show_ticket_link():
    """Карточка выдачи и уведомление подписки используют Offer.url с текстом 'купить билет'."""
    offer = Offer(
        site="kupibilet",
        price=5000,
        currency="RUB",
        airline="SCAT",
        depart_time="10:00",
        arrive_time="14:00",
        url="https://www.kupibilet.ru/search?ticket=1797714000000-3F-3316",
        url_is_direct=True,
    )
    q = Query("MOW", "EVN", date(2026, 12, 20))

    assert offer_link_caption(offer) == "купить билет"
    link_html = offer_link_html(offer)
    assert link_html is not None
    assert 'href="https://www.kupibilet.ru/search?ticket=1797714000000-3F-3316"' in link_html
    assert "купить билет" in link_html

    # Карточка выдачи
    card_lines = offer_card(offer, q, number=1)
    card_text = "\n".join(card_lines)
    assert "купить билет" in card_text
    assert "https://www.kupibilet.ru/search?ticket=1797714000000-3F-3316" in card_text
    assert "Найдите рейс в выдаче по" not in card_text

    # Детали
    details = format_details(offer, q)
    assert "купить билет" in details
    assert "Найдите рейс в выдаче по" not in details

    # Уведомление подписки
    sub = Subscription(
        id=1,
        chat_id=100,
        origin="MOW",
        destination="EVN",
        depart=date(2026, 12, 20),
        adults=1,
        created_at=0.0,
    )
    notif = build_notification(sub, offer, ["подешевела на 500 ₽"])
    assert "купить билет" in notif
    assert "https://www.kupibilet.ru/search?ticket=1797714000000-3F-3316" in notif
