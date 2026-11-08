"""Тесты парсера City Travel (city.travel), построения URL и конфигурации."""
from __future__ import annotations

from datetime import date
import pytest

from avia.collector import Snapshot
from avia.config import load_settings
from avia.formatting import site_label
from avia.models import Query
from avia.parsers import PARSERS
from avia.parsers.city_travel import parse_city_travel, results_complete
from avia.sites import build_url

SAMPLE_TEXT_ONEWAY = """
Лучшая цена
Сначала дешёвый
Фильтр
FlyOne Armenia
10:15
20 дек
KUF
3 ч 15 мин
прямой
EVN
13:30
20 дек
1 пассажир
14 538 ₽
Utair
23:15
20 дек
KUF
27 ч 15 мин
1 пересадка
EVN
02:30
22 дек
Москва, 21ч 50м
1 пассажир
22 912 ₽
Аэрофлот
06:10
20 дек
KUF
11 ч 20 мин
1 пересадка
EVN
18:30
20 дек
Москва, 6ч 10м
1 пассажир
25 400 ₽
S7 Airlines
14:20
20 дек
KUF
15 ч 40 мин
1 пересадка
EVN
07:00
21 дек
Новосибирск, 8ч 30м
1 пассажир
27 150 ₽
Red Wings
19:00
20 дек
KUF
4 ч 10 мин
прямой
EVN
00:10
21 дек
1 пассажир
31 800 ₽
ПОЛЬЗОВАТЕЛЯМ
О компании
Контакты
Правила сервиса
"""

SAMPLE_TEXT_ROUNDTRIP = """
Лучшая цена
Сначала дешёвый
Фильтр
Pobeda
17:50
12 дек
KUF
1 ч 55 мин
прямой
SVO
18:45
12 дек
07:40
15 дек
SVO
1 ч 50 мин
прямой
KUF
10:30
15 дек
Туда и обратно, 1 пассажир
5 918 ₽
ПОЛЬЗОВАТЕЛЯМ
"""

SAMPLE_TEXT_RT_MULTIPLE_AIRLINES = """
Utair, Pobeda
08:20
12 дек
KUF
4 ч 15 мин
1 пересадка
SVO
11:35
12 дек
Москва, 1ч 30м
16:10
15 дек
SVO
1 ч 50 мин
прямой
KUF
19:00
15 дек
Туда и обратно, 1 пассажир
8 450 ₽
ПОЛЬЗОВАТЕЛЯМ
"""


def test_city_travel_parser_registered():
    """Парсер city_travel зарегистрирован в реестре и имеет функцию results_complete."""
    assert "city_travel" in PARSERS
    assert PARSERS["city_travel"] is parse_city_travel
    assert getattr(parse_city_travel, "results_complete", None) is results_complete


def test_parse_city_travel_oneway_5_offers():
    """В одну сторону: парсинг 5 офферов с нужными ценами (14538, 22912 и др.), проверка полей."""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 20), adults=1)
    snap = Snapshot(
        url="https://city.travel/avia/search/KUF/EVN/E/20.12.2026/0/1-0-0/0/0/0/00000000000000000000000000000000",
        text=SAMPLE_TEXT_ONEWAY,
    )
    offers = parse_city_travel(snap, q)

    assert len(offers) == 5
    prices = [o.price for o in offers]
    assert prices == [14538, 22912, 25400, 27150, 31800]
    assert offers[0].price == 14538
    assert offers[1].price == 22912

    # Все офферы относятся к city_travel и не имеют обратного плеча
    assert all(o.site == "city_travel" for o in offers)
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.back is None for o in offers)

    # Первый оффер (FlyOne Armenia, прямой)
    first = offers[0]
    assert first.airline == "FlyOne Armenia"
    assert first.depart_time == "10:15"
    assert first.arrive_time == "13:30"
    assert first.duration == "3ч 15м"
    assert first.transfers == 0
    assert first.layovers is None
    assert first.depart_at == "2026-12-20T10:15"
    assert first.arrive_at == "2026-12-20T13:30"
    assert first.url is not None and "city.travel" in first.url
    assert first.offer_id and len(first.offer_id) == 12

    # Второй оффер (Utair, с 1 пересадкой со сменой дня)
    second = offers[1]
    assert second.airline == "Utair"
    assert second.depart_time == "23:15"
    assert second.arrive_time == "02:30"
    assert second.duration == "27ч 15м"
    assert second.transfers == 1
    assert second.layovers == ["Москва, 21ч 50м"]
    assert second.depart_at == "2026-12-20T23:15"
    assert second.arrive_at == "2026-12-22T02:30"


def test_parse_city_travel_roundtrip():
    """Туда-обратно: цена 5918 ₽, проверка обоих плеч и их полей."""
    q = Query(
        origin="KUF",
        destination="SVO",
        depart=date(2026, 12, 12),
        return_date=date(2026, 12, 15),
        adults=1,
    )
    snap = Snapshot(
        url="https://city.travel/avia/search/KUF/MOW/E/12.12.2026/15.12.2026/1-0-0/0/0/0/00000000000000000000000000000000?rt=RTAMOW",
        text=SAMPLE_TEXT_ROUNDTRIP,
    )
    offers = parse_city_travel(snap, q)

    assert len(offers) == 1
    o = offers[0]
    assert o.price == 5918
    assert o.airline == "Pobeda"
    assert o.site == "city_travel"
    assert o.currency == "RUB"

    # Первое плечо
    assert o.depart_time == "17:50"
    assert o.arrive_time == "18:45"
    assert o.duration == "1ч 55м"
    assert o.transfers == 0
    assert o.layovers is None
    assert o.depart_at == "2026-12-12T17:50"
    assert o.arrive_at == "2026-12-12T18:45"

    # Обратное плечо
    back = o.back
    assert back is not None
    assert back.airline == "Pobeda"
    assert back.depart_time == "07:40"
    assert back.arrive_time == "10:30"
    assert back.duration == "1ч 50м"
    assert back.transfers == 0
    assert back.layovers is None
    assert back.depart_at == "2026-12-15T07:40"
    assert back.arrive_at == "2026-12-15T10:30"


def test_parse_city_travel_roundtrip_multiple_airlines():
    """Туда-обратно с несколькими перевозчиками: «Utair, Pobeda»."""
    q = Query(
        origin="KUF",
        destination="SVO",
        depart=date(2026, 12, 12),
        return_date=date(2026, 12, 15),
        adults=1,
    )
    snap = Snapshot(
        url="",
        text=SAMPLE_TEXT_RT_MULTIPLE_AIRLINES,
    )
    offers = parse_city_travel(snap, q)
    assert len(offers) == 1
    o = offers[0]
    assert o.price == 8450
    assert o.airline == "Utair, Pobeda"
    assert o.transfers == 1
    assert o.layovers == ["Москва, 1ч 30м"]
    assert o.back is not None
    assert o.back.airline == "Pobeda"
    assert o.back.transfers == 0


def test_build_url_city_travel_both_templates():
    """Проверка построения URL по обоим шаблонам city_travel из config/sites.yaml."""
    settings = load_settings()
    site = next((s for s in settings.sites if s.name == "city_travel"), None)
    assert site is not None
    assert site.stage == "trial"

    # В одну сторону
    q_ow = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 20), adults=1)
    url_ow = build_url(site, q_ow)
    assert (
        url_ow
        == "https://city.travel/avia/search/KUF/EVN/E/20.12.2026/0/1-0-0/0/0/0/00000000000000000000000000000000"
    )

    # Туда-обратно
    q_rt = Query(
        origin="KUF",
        destination="MOW",
        depart=date(2026, 12, 12),
        return_date=date(2026, 12, 15),
        adults=1,
    )
    url_rt = build_url(site, q_rt)
    assert (
        url_rt
        == "https://city.travel/avia/search/KUF/MOW/E/12.12.2026/15.12.2026/1-0-0/0/0/0/00000000000000000000000000000000?rt=RTAMOW"
    )


def test_city_travel_config_loading():
    """Загрузка конфигурации city_travel: stage trial, source browser, phone-поля, settle_ms."""
    settings = load_settings()
    site = next((s for s in settings.sites if s.name == "city_travel"), None)
    assert site is not None
    assert site.stage == "trial"
    assert site.source == "browser"
    assert site.parser == "city_travel"
    assert site.phone == "galaxya12,huawei2"
    assert site.phone_wait_seconds == 25
    assert site.settle_ms == 15000
    assert site.phone_capture_json == []
    assert site.enabled is True


def test_formatting_site_label():
    """Подпись сайта city_travel в formatting.py -> «City Travel»."""
    assert site_label("city_travel") == "City Travel"


def test_results_complete_city_travel():
    """Проверка results_complete для City Travel."""
    snap_ow = Snapshot(url="", text=SAMPLE_TEXT_ONEWAY)
    assert results_complete(snap_ow) is True
    assert results_complete(None) is False
    assert results_complete(Snapshot(url="", text="")) is False
    assert results_complete(Snapshot(url="", text="нет цен", blocked=True)) is False


def test_corrupted_cards_skipped_resilience():
    """Битые карточки пропускаются, валидные остаются."""
    corrupted_text = """
FlyOne Armenia
10:15
20 дек
KUF
3 ч 15 мин
прямой
EVN
13:30
20 дек
1 пассажир
14 538 ₽
BrokenAirline
11:00
20 дек
KUF
EVN
неполные данные
BrokenAirline2
12:00
20 дек
KUF
2 ч
прямой
EVN
14:00
20 дек
1 пассажир
нет_цены_тут
Utair
23:15
20 дек
KUF
27 ч 15 мин
1 пересадка
EVN
02:30
22 дек
Москва, 21ч 50м
1 пассажир
22 912 ₽
ПОЛЬЗОВАТЕЛЯМ
"""
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 20), adults=1)
    snap = Snapshot(url="", text=corrupted_text)
    offers = parse_city_travel(snap, q)
    assert len(offers) == 2
    assert offers[0].price == 14538
    assert offers[1].price == 22912
