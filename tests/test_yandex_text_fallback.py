"""Тесты запасного разбора Яндекс Путешествий по тексту страницы (text fallback)."""
from datetime import date
import logging
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.offers import compute_offer_id
from avia.parsers.yandex_travel import (
    _parse_yandex_travel_text,
    parse_yandex_travel,
    results_complete,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "yandex_travel"
TEXT_ONEWAY_1012 = (FIXTURES_DIR / "text_only_oneway_mow_led_1012_page.txt").read_text(encoding="utf-8")
JSON_CONT0 = (FIXTURES_DIR / "results_cont0_mow_ala.json").read_text(encoding="utf-8")
API_URL = "https://travel.yandex.ru/api/avia/search/results?qid=test_qid&cont=0"


def _q_ow(depart: date = date(2026, 12, 10)) -> Query:
    """Запрос в одну сторону MOW -> LED."""
    return Query(origin="MOW", destination="LED", depart=depart, adults=1)


def _q_rt() -> Query:
    """Запрос туда-обратно MOW -> LED (10.12.2026 – 17.12.2026)."""
    return Query(
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
        return_date=date(2026, 12, 17),
        adults=1,
    )


def test_text_only_oneway_mow_led_1012():
    """Разбор text_only_oneway_mow_led_1012: 12 офферов, мин. цена 3 663 ₽, сортировка."""
    snap = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[],
    )
    offers = parse_yandex_travel(snap, _q_ow(date(2026, 12, 10)))

    assert len(offers) == 12
    assert offers[0].price == 3663
    assert min(o.price for o in offers) == 3663
    assert [o.price for o in offers] == sorted(o.price for o in offers)

    expected_prices = [3663, 3894, 3894, 3894, 3896, 5282, 5318, 5318, 5318, 5318, 5318, 5318]
    assert [o.price for o in offers] == expected_prices

    assert all(o.site == "yandex_travel" for o in offers)
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.back is None for o in offers)
    assert all(o.url is None for o in offers)
    assert all(o.baggage == "без багажа" for o in offers)
    assert all(o.offer_id == compute_offer_id(o) for o in offers)

    # Проверка первого предложения (S7 Airlines, DME -> LED)
    first = offers[0]
    assert first.airline == "S7 Airlines"
    assert first.depart_time == "16:00"
    assert first.arrive_time == "17:45"
    assert first.duration == "1ч 45м"
    assert first.transfers == 0
    assert first.baggage == "без багажа"
    assert first.carry_on is None
    assert first.depart_at == "2026-12-10T16:00"
    assert first.arrive_at == "2026-12-10T17:45"
    assert first.back is None

    # Проверка второго предложения (Победа с ручной кладью)
    second = offers[1]
    assert second.airline == "Победа"
    assert second.depart_time == "08:30"
    assert second.arrive_time == "10:05"
    assert second.duration == "1ч 35м"
    assert second.transfers == 0
    assert second.price == 3894
    assert second.carry_on == "ручная кладь есть"
    assert second.baggage == "без багажа"

    # Проверка состава авиакомпаний
    airlines = [o.airline for o in offers]
    assert airlines.count("S7 Airlines") == 1
    assert airlines.count("Победа") == 4
    assert airlines.count("Россия") == 7
    assert set(airlines) == {"S7 Airlines", "Победа", "Россия"}

    # Ручная кладь указана у Победы, но отсутствует у S7 и России
    assert all(o.carry_on == "ручная кладь есть" for o in offers if o.airline == "Победа")
    assert all(o.carry_on is None for o in offers if o.airline in ("S7 Airlines", "Россия"))


def test_hotels_and_neighbor_dates_excluded():
    """Цены отелей (1920..4200 ₽), соседних дат (3394 ₽) и шапки (5793 ₽) не попадают в офферы."""
    snap = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[],
    )
    offers = parse_yandex_travel(snap, _q_ow())

    extracted_prices = {o.price for o in offers}
    noise_prices = {1920, 2400, 1518, 1598, 3591, 4200, 2511, 2790, 3394, 5793}
    assert not (extracted_prices & noise_prices)


def test_deduplication_of_ad_duplicates():
    """Дубликат карточки с меткой РЕКЛАМА схлопывается."""
    # Дублируем первую карточку S7 с рекламной меткой
    ad_duplicate = (
        "\nРЕКЛАМА\n"
        "лучшая цена\n"
        "S7 Airlines\n"
        "16:00\n"
        "1 ч 45 м\n"
        "17:45\n"
        "DME\n"
        "прямой\n"
        "LED\n"
        "3 663 ₽\n"
    )
    snap = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012 + ad_duplicate,
        json_payloads=[],
    )
    offers = parse_yandex_travel(snap, _q_ow())
    assert len(offers) == 12


def test_round_trip_returns_empty():
    """Обратные билеты (Query.return_date) дают пустой список (RT в тексте не подтверждён)."""
    snap = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[],
    )
    assert parse_yandex_travel(snap, _q_rt()) == []


def test_json_priority_over_text(caplog):
    """JSON-путь приоритетнее текстового; text fallback вызывается только при отсутствии JSON-офферов."""
    snap_with_json = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[(API_URL, JSON_CONT0)],
    )
    q_ala = Query("MOW", "ALA", date(2026, 12, 12))
    with caplog.at_level(logging.INFO, logger="avia.parsers.yandex_travel"):
        offers = parse_yandex_travel(snap_with_json, q_ala)

    # Должны вернуться 6 офферов из JSON (MOW -> ALA)
    assert len(offers) == 6
    assert offers[0].price == 17672
    assert "разобрано" not in caplog.text

    # Когда JSON пуст, срабатывает text fallback и пишет INFO-лог
    snap_only_text = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[],
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="avia.parsers.yandex_travel"):
        text_offers = parse_yandex_travel(snap_only_text, _q_ow())

    assert len(text_offers) == 12
    assert "yandex: разобрано 12 карточек из текста страницы (нет JSON)" in caplog.text


def test_empty_and_garbage_text_returns_empty():
    """Пустой и мусорный текст возвращает пустой список."""
    snap_empty = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text="",
        json_payloads=[],
    )
    assert parse_yandex_travel(snap_empty, _q_ow()) == []

    snap_garbage = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text="Случайная страница\nКонтакты\nО компании\n12345",
        json_payloads=[],
    )
    assert parse_yandex_travel(snap_garbage, _q_ow()) == []


def test_blocked_snapshot_returns_empty():
    """При snapshot.blocked=True возвращается пустой список."""
    snap_blocked = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[],
        blocked=True,
    )
    assert parse_yandex_travel(snap_blocked, _q_ow()) == []


def test_results_complete_for_text_and_partners():
    """results_complete остаётся False при разборе текста и при неполном опросе партнёров."""
    snap_text = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=[],
    )
    assert results_complete(snap_text) is False

    snap_none = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text=TEXT_ONEWAY_1012,
        json_payloads=None,
    )
    assert results_complete(snap_none) is False

    # В тексте есть "Опрашиваем партнёров: 5 из 20" -> False в любом случае
    snap_survey = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/",
        text="Опрашиваем партнёров: 5 из 20\nРезультаты поиска",
        json_payloads=[],
    )
    assert results_complete(snap_survey) is False


def test_transfers_variations():
    """Распознавание пересадок: '1 пересадка', 'пересадка в XXX', пропуск неизвестных форматов."""
    card_template = (
        "Аэрофлот\n"
        "10:00\n"
        "5 ч 00 м\n"
        "15:00\n"
        "SVO\n"
        "{transfers}\n"
        "LED\n"
        "7 000 ₽\n"
    )

    # 1. 1 пересадка
    text1 = card_template.format(transfers="1 пересадка")
    offers1 = _parse_yandex_travel_text(text1, _q_ow())
    assert len(offers1) == 1
    assert offers1[0].transfers == 1
    assert offers1[0].layovers is None

    # 2. пересадка в Казани
    text2 = card_template.format(transfers="пересадка в Казани")
    offers2 = _parse_yandex_travel_text(text2, _q_ow())
    assert len(offers2) == 1
    assert offers2[0].transfers == 1
    assert offers2[0].layovers == ["Казани"]

    # 3. неизвестный формат пересадки -> пропускаем карточку
    text3 = card_template.format(transfers="сложный составной маршрут через неизвестный пункт")
    offers3 = _parse_yandex_travel_text(text3, _q_ow())
    assert len(offers3) == 0
