"""Тесты парсера Т-Путешествия (ttravel) и построения URL."""
from __future__ import annotations

from datetime import date
import json
import logging
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.config import load_settings
from avia.models import Query
from avia.parsers import PARSERS
from avia.parsers.ttravel import parse_ttravel, results_complete
from avia.sites import build_url

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ttravel"
OW_FIXTURE_PATH = FIXTURES_DIR / "search_ow_kuf_mow_1210.json"
RT_FIXTURE_PATH = FIXTURES_DIR / "search_rt_kuf_svo_1210_1217.json"
STREAMING_URL = "https://www.tbank-online.com/api/travel/flight/search/startStreaming?sessionId=test"


def _make_ow_snapshot() -> Snapshot:
    text = OW_FIXTURE_PATH.read_text(encoding="utf-8")
    return Snapshot(
        url="https://www.tbank-online.com/travel/flights/one-way/KUF-MOW/12-10/?adults=1&children=0&infants=0&cabin=Y&composite=0",
        text="Т-Путешествия поиск авиабилетов",
        json_payloads=[(STREAMING_URL, text)],
    )


def _make_rt_snapshot() -> Snapshot:
    text = RT_FIXTURE_PATH.read_text(encoding="utf-8")
    return Snapshot(
        url="https://www.tbank-online.com/travel/flights/multi-way/KUF-SVO/12-10/SVO-KUF/12-17/?adults=1&children=0&infants=0&cabin=Y&composite=0",
        text="Т-Путешествия поиск авиабилетов туда-обратно",
        json_payloads=[(STREAMING_URL, text)],
    )


def test_ttravel_parser_registered():
    """Парсер ttravel зарегистрирован в реестре и имеет функцию results_complete."""
    assert "ttravel" in PARSERS
    assert PARSERS["ttravel"] is parse_ttravel
    assert getattr(parse_ttravel, "results_complete", None) is results_complete


def test_parse_ttravel_ow_sample():
    """Парсинг фикстуры в одну сторону (OW): количество > 0, мин. цена 3176 ₽, без обратного рейса."""
    q = Query(origin="KUF", destination="MOW", depart=date(2026, 12, 10), adults=1)
    snap = _make_ow_snapshot()
    offers = parse_ttravel(snap, q)

    assert len(offers) > 0
    assert len(offers) == 60

    # Минимальная цена 3176 ₽
    min_price = min(o.price for o in offers)
    assert min_price == 3176
    assert offers[0].price == 3176

    # Все офферы относятся к ttravel и не имеют обратного плеча
    assert all(o.site == "ttravel" for o in offers)
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.back is None for o in offers)

    # Проверка полей самого дешёвого предложения (S7 Airlines)
    first = offers[0]
    assert first.airline == "S7 Airlines"
    assert first.depart_time == "20:50"
    assert first.arrive_time == "21:40"
    assert first.duration == "1ч 50м"
    assert first.transfers == 0
    assert first.baggage == "без багажа"
    assert first.carry_on == "ручная кладь есть"
    assert first.flight_numbers == ["S7 1074"]
    assert first.aircraft == "Airbus A320"
    assert first.layovers is None
    assert first.depart_at == "2026-12-10T20:50"
    assert first.arrive_at == "2026-12-10T21:40"
    assert first.url is not None and "tbank-online.com/travel/flights/one-way" in first.url
    assert first.offer_id and len(first.offer_id) == 12


def test_parse_ttravel_rt_sample():
    """Парсинг фикстуры туда-обратно (RT): количество > 0, мин. цена 5970 ₽, с обратным рейсом."""
    q = Query(origin="KUF", destination="SVO", depart=date(2026, 12, 10), return_date=date(2026, 12, 17), adults=1)
    snap = _make_rt_snapshot()
    offers = parse_ttravel(snap, q)

    assert len(offers) > 0
    assert len(offers) == 191

    # Минимальная цена 5970 ₽
    min_price = min(o.price for o in offers)
    assert min_price == 5970
    assert offers[0].price == 5970

    # Все офферы относятся к ttravel и имеют обратное плечо
    assert all(o.site == "ttravel" for o in offers)
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.back is not None for o in offers)

    # Проверка полей самого дешёвого предложения (S7 туда и обратно)
    first = offers[0]
    assert first.airline == "S7 Airlines"
    assert first.depart_time == "20:50"
    assert first.arrive_time == "21:40"
    assert first.duration == "1ч 50м"
    assert first.transfers == 0
    assert first.baggage == "без багажа"
    assert first.flight_numbers == ["S7 1074"]
    assert first.aircraft == "Airbus A320"
    assert first.depart_at == "2026-12-10T20:50"
    assert first.arrive_at == "2026-12-10T21:40"
    assert first.url is not None and "tbank-online.com/travel/flights/multi-way" in first.url

    # Обратный рейс
    back = first.back
    assert back is not None
    assert back.airline == "S7 Airlines"
    assert back.depart_time == "17:15"
    assert back.arrive_time == "20:05"
    assert back.duration == "1ч 50м"
    assert back.transfers == 0
    assert back.flight_numbers == ["S7 1073"]
    assert back.aircraft == "Airbus A320"
    assert back.depart_at == "2026-12-17T17:15"
    assert back.arrive_at == "2026-12-17T20:05"


def test_parse_ttravel_corrupted_offer_does_not_break_parsing(caplog):
    """Битый оффер в выдаче пропускается и не ломает разбор остальных."""
    data = json.loads(OW_FIXTURE_PATH.read_text(encoding="utf-8"))
    # Добавляем повреждённые офферы
    data["payload"]["offers"].insert(0, {"price": None, "flights": [0]})  # без цены
    data["payload"]["offers"].insert(1, {"price": {"amount": "1000", "currency": "RUB"}, "flights": []})  # без рейсов
    data["payload"]["offers"].insert(2, {"price": {"amount": "invalid", "currency": "RUB"}, "flights": [0]})  # цена не число
    data["payload"]["offers"].insert(3, {"price": {"amount": "2000", "currency": "RUB"}, "flights": [99999]})  # несуществующий индекс
    data["payload"]["offers"].insert(4, "not_a_dict")  # мусор вместо объекта

    snap = Snapshot(
        url="https://www.tbank-online.com/travel/flights/one-way/KUF-MOW/12-10/",
        text="",
        json_payloads=[(STREAMING_URL, json.dumps(data))],
    )
    q = Query(origin="KUF", destination="MOW", depart=date(2026, 12, 10), adults=1)

    with caplog.at_level(logging.WARNING, logger="avia.parsers.ttravel"):
        offers = parse_ttravel(snap, q)

    assert len(offers) == 60
    assert offers[0].price == 3176


def test_parse_ttravel_empty_or_invalid_snapshot():
    """Пустые или повреждённые снимки возвращают пустой список."""
    q = Query(origin="KUF", destination="MOW", depart=date(2026, 12, 10), adults=1)
    assert parse_ttravel(None, q) == []
    assert parse_ttravel(Snapshot(url="", text=""), q) == []
    assert parse_ttravel(Snapshot(url="", text="", json_payloads=[]), q) == []
    assert parse_ttravel(Snapshot(url="", text="", json_payloads=[("https://other.com", "{}")]), q) == []
    assert parse_ttravel(Snapshot(url="", text="", json_payloads=[(STREAMING_URL, "invalid json")]), q) == []
    assert parse_ttravel(Snapshot(url="", text="", json_payloads=[(STREAMING_URL, json.dumps({"payload": None}))]), q) == []


def test_build_url_ttravel():
    """Построение URL для сайта ttravel из config/sites.yaml для обоих режимов."""
    settings = load_settings()
    site = next((s for s in settings.sites if s.name == "ttravel"), None)
    assert site is not None
    assert site.stage == "trial"

    # В одну сторону
    q_ow = Query(origin="KUF", destination="MOW", depart=date(2026, 12, 10), adults=1)
    url_ow = build_url(site, q_ow)
    assert url_ow == "https://www.tbank-online.com/travel/flights/one-way/KUF-MOW/12-10/?adults=1&children=0&infants=0&cabin=Y&composite=0"

    # Туда-обратно
    q_rt = Query(origin="KUF", destination="SVO", depart=date(2026, 12, 10), return_date=date(2026, 12, 17), adults=2)
    url_rt = build_url(site, q_rt)
    assert url_rt == "https://www.tbank-online.com/travel/flights/multi-way/KUF-SVO/12-10/SVO-KUF/12-17/?adults=2&children=0&infants=0&cabin=Y&composite=0"


def test_results_complete_ttravel():
    """Проверка эмпирического признака полноты выдачи results_complete."""
    snap_ow = _make_ow_snapshot()
    snap_rt = _make_rt_snapshot()
    assert results_complete(snap_ow) is True
    assert results_complete(snap_rt) is True
    assert results_complete(None) is False
    assert results_complete(Snapshot(url="", text="", json_payloads=[])) is False
    assert results_complete(Snapshot(url="", text="", json_payloads=[(STREAMING_URL, "{}")])) is False
