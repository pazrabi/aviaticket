"""Тесты запасного разбора Ozon Travel по тексту страницы (text fallback)."""
from datetime import date
import logging
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.offers import compute_offer_id
from avia.parsers.ozon_travel import parse_ozon_travel, results_complete

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ozon_travel"
WIDGET_URL = (FIXTURES_DIR / "widget_url.txt").read_text(encoding="utf-8").strip()
WIDGET_JSON = (FIXTURES_DIR / "search_oneway_kuf_evn_0112_widget.json").read_text(encoding="utf-8")

TEXT_ONEWAY_0112 = (FIXTURES_DIR / "text_only_oneway_kuf_evn_0112_page.txt").read_text(encoding="utf-8")
TEXT_ONEWAY_0412 = (FIXTURES_DIR / "text_only_oneway_kuf_evn_0412_page.txt").read_text(encoding="utf-8")
TEXT_RT_0112_0812 = (FIXTURES_DIR / "text_rt_kuf_evn_0112_0812_page.txt").read_text(encoding="utf-8")
PAGE_TEXT_0112 = (FIXTURES_DIR / "search_oneway_kuf_evn_0112_page.txt").read_text(encoding="utf-8")


def _q_ow(depart: date = date(2026, 12, 1)) -> Query:
    """Запрос в одну сторону KUF -> EVN."""
    return Query(origin="KUF", destination="EVN", depart=depart, adults=1)


def _q_rt() -> Query:
    """Запрос туда-обратно KUF -> EVN (01.12.2026 – 08.12.2026)."""
    return Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 1),
        return_date=date(2026, 12, 8),
        adults=1,
    )


def test_text_only_oneway_kuf_evn_0112():
    """Разбор text_only_oneway_kuf_evn_0112: минимальная цена 11 631 ₽, карточки без багажа."""
    snap = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_ONEWAY_0112,
        json_payloads=[],
    )
    offers = parse_ozon_travel(snap, _q_ow(date(2026, 12, 1)))

    assert len(offers) == 22
    assert offers[0].price == 11631
    assert min(o.price for o in offers) == 11631
    assert [o.price for o in offers] == sorted(o.price for o in offers)
    assert all(o.site == "ozon_travel" for o in offers)
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.back is None for o in offers)
    assert all(o.url is None for o in offers)
    assert all(o.offer_id == compute_offer_id(o) for o in offers)

    # Проверка первого предложения
    first = offers[0]
    assert first.airline == "Победа, Аэрофлот"
    assert first.depart_time == "17:35"
    assert first.arrive_time == "23:15"
    assert first.depart_at == "2026-12-01T17:35"
    assert first.arrive_at == "2026-12-02T23:15"
    assert first.duration == "29ч 40м"
    assert first.transfers == 1
    assert first.layovers == ["Москва, 24ч"]
    assert first.baggage == "без багажа"


def test_text_only_oneway_kuf_evn_0412_direct_flight():
    """Разбор text_only_oneway_kuf_evn_0412: прямой рейс Аэрофлота за 12 426 ₽ (14:40 – 17:30, 2ч 50м)."""
    snap = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_ONEWAY_0412,
        json_payloads=[],
    )
    offers = parse_ozon_travel(snap, _q_ow(date(2026, 12, 4)))

    assert len(offers) == 8
    assert offers[0].price == 12426
    assert min(o.price for o in offers) == 12426

    direct = [o for o in offers if o.transfers == 0]
    assert len(direct) == 1
    d = direct[0]
    assert d.price == 12426
    assert d.airline == "Аэрофлот"
    assert d.depart_time == "14:40"
    assert d.arrive_time == "17:30"
    assert d.duration == "2ч 50м"
    assert d.transfers == 0
    assert d.layovers is None
    assert d.depart_at == "2026-12-04T14:40"
    assert d.arrive_at == "2026-12-04T17:30"
    assert d.baggage == "без багажа"
    assert d.back is None


def test_text_rt_kuf_evn_0112_0812_round_trip():
    """Разбор туда-обратно: 12 офферов, минимальная цена 24 989 ₽, у всех есть back с датой 2026-12-08."""
    snap = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_RT_0112_0812,
        json_payloads=[],
    )
    offers = parse_ozon_travel(snap, _q_rt())

    assert len(offers) == 12
    assert offers[0].price == 24989
    assert min(o.price for o in offers) == 24989
    assert all(o.back is not None for o in offers)

    first = offers[0]
    assert first.depart_at == "2026-12-01T17:35"
    assert first.arrive_at == "2026-12-02T14:00"
    assert first.back is not None
    assert first.back.depart_at == "2026-12-08T20:05"
    assert first.back.arrive_at == "2026-12-09T05:30"
    assert first.back.depart_at.startswith("2026-12-08")
    assert first.back.duration == "9ч 25м"
    assert first.back.transfers == 1
    assert first.back.layovers == ["Минеральные Воды, 5.5ч"]


def test_search_oneway_kuf_evn_0112_comparison_with_json():
    """Сверка текстового разбора search_oneway_kuf_evn_0112 с разбором JSON."""
    snap_json = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=PAGE_TEXT_0112,
        json_payloads=[(WIDGET_URL, WIDGET_JSON)],
    )
    json_offers = parse_ozon_travel(snap_json, _q_ow())

    snap_text = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=PAGE_TEXT_0112,
        json_payloads=[],
    )
    text_offers = parse_ozon_travel(snap_text, _q_ow())

    assert len(text_offers) == 2
    json_prices = {o.price for o in json_offers}
    assert all(o.price in json_prices for o in text_offers)

    for t in text_offers:
        j_match = next(
            (
                j
                for j in json_offers
                if j.depart_time == t.depart_time
                and j.arrive_time == t.arrive_time
                and j.duration == t.duration
            ),
            None,
        )
        assert j_match is not None, f"Не найден совпадающий JSON-оффер для {t}"
        assert t.price == j_match.price
        assert t.airline == j_match.airline
        assert t.transfers == j_match.transfers
        assert t.layovers == j_match.layovers
        assert t.baggage == j_match.baggage


def test_query_type_filtering():
    """Поиск с return_date не берёт односторонние карточки и наоборот."""
    snap_ow = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_ONEWAY_0112,
        json_payloads=[],
    )
    # Запрос туда-обратно на одностороннем тексте даёт []
    assert parse_ozon_travel(snap_ow, _q_rt()) == []

    snap_rt = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_RT_0112_0812,
        json_payloads=[],
    )
    # Запрос в одну сторону на тексте туда-обратно даёт []
    assert parse_ozon_travel(snap_rt, _q_ow()) == []


def test_blocked_snapshot_returns_empty():
    """При snapshot.blocked возвращается пустой список."""
    snap_blocked_ow = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_ONEWAY_0112,
        json_payloads=[],
        blocked=True,
    )
    assert parse_ozon_travel(snap_blocked_ow, _q_ow()) == []

    snap_blocked_rt = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_RT_0112_0812,
        json_payloads=[],
        blocked=True,
    )
    assert parse_ozon_travel(snap_blocked_rt, _q_rt()) == []


def test_garbage_text_returns_empty():
    """Мусорный или пустой текст даёт пустой список."""
    snap_garbage = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text="Случайная страница\nКонтакты\nО компании\n12345",
        json_payloads=[],
    )
    assert parse_ozon_travel(snap_garbage, _q_ow()) == []

    snap_empty = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text="",
        json_payloads=[],
    )
    assert parse_ozon_travel(snap_empty, _q_ow()) == []


def test_text_fallback_not_invoked_when_json_succeeds(caplog):
    """Text fallback не включается, если JSON дал офферы; логируется только при разборе текста."""
    snap_json = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=PAGE_TEXT_0112,
        json_payloads=[(WIDGET_URL, WIDGET_JSON)],
    )
    with caplog.at_level(logging.INFO, logger="avia.parsers.ozon_travel"):
        offers = parse_ozon_travel(snap_json, _q_ow())
    assert len(offers) == 30
    assert "разобрано" not in caplog.text

    # Когда JSON пуст, срабатывает text fallback и пишет INFO-лог
    snap_text = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=PAGE_TEXT_0112,
        json_payloads=[],
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="avia.parsers.ozon_travel"):
        text_offers = parse_ozon_travel(snap_text, _q_ow())
    assert len(text_offers) == 2
    assert "ozon: разобрано 2 карточек из текста страницы (нет JSON)" in caplog.text


def test_results_complete_remains_false_for_text():
    """results_complete остаётся False при отсутствии валидных JSON-ответов."""
    snap_text = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_ONEWAY_0112,
        json_payloads=[],
    )
    assert results_complete(snap_text) is False

    snap_none = Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=TEXT_RT_0112_0812,
        json_payloads=None,
    )
    assert results_complete(snap_none) is False
