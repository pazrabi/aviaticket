"""Тесты склеивания дубликатов рейсов с разных сайтов в одну карточку."""
from __future__ import annotations

from datetime import date

import pytest

from avia.criteria import SearchCriteria
from avia.formatting import build_compact_report, offer_card
from avia.models import Leg, Offer, OfferAlt, Query, SiteResult
from avia.offers import flight_key, merge_same_flights, normalize_airline, same_flight


def test_merge_three_sites_same_flight():
    """(а) один рейс с трёх сайтов → один билет с самой низкой ценой и двумя alts по возрастанию."""
    o_avia = Offer(
        site="aviasales",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7 Airlines",
        url="https://aviasales.ru/ticket1",
        url_is_direct=True,
    )
    o_kupi = Offer(
        site="kupibilet",
        price=2500,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7 Airlines",
        url="https://kupibilet.ru/ticket2",
        url_is_direct=False,
    )
    o_tutu = Offer(
        site="tutu",
        price=2700,
        depart_time="10:00",
        arrive_time="12:00",
        airline="s7",
        url="https://tutu.ru/ticket3",
        url_is_direct=False,
    )

    merged = merge_same_flights([o_avia, o_kupi, o_tutu])
    assert len(merged) == 1

    rep = merged[0]
    # Представитель самый дешёвый — Купибилет (2500)
    assert rep.site == "kupibilet"
    assert rep.price == 2500
    assert rep.url == "https://kupibilet.ru/ticket2"
    assert rep.url_is_direct is False
    assert rep.alts is not None
    assert len(rep.alts) == 2

    # alts отсортированы по возрастанию цены: tutu (2700), затем aviasales (3000)
    assert rep.alts[0] == OfferAlt(
        site="tutu",
        price=2700,
        currency="RUB",
        url="https://tutu.ru/ticket3",
        url_is_direct=False,
    )
    assert rep.alts[1] == OfferAlt(
        site="aviasales",
        price=3000,
        currency="RUB",
        url="https://aviasales.ru/ticket1",
        url_is_direct=True,
    )


def test_different_times_not_merged():
    """(б) разные времена → не склеиваются."""
    o1 = Offer(
        site="aviasales",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7",
    )
    o2 = Offer(
        site="kupibilet",
        price=2500,
        depart_time="10:15",
        arrive_time="12:00",
        airline="S7",
    )
    o3 = Offer(
        site="tutu",
        price=2700,
        depart_time="10:00",
        arrive_time="12:15",
        airline="S7",
    )

    assert flight_key(o1) != flight_key(o2)
    assert flight_key(o1) != flight_key(o3)
    assert not same_flight(o1, o2)
    assert not same_flight(o1, o3)

    merged = merge_same_flights([o1, o2, o3])
    assert len(merged) == 3
    for o in merged:
        assert o.alts is None


def test_airline_compatibility():
    """(в) разные авиакомпании при тех же временах → не склеиваются, одна авиакомпания пустая → склеиваются."""
    # 1. Разные авиакомпании при одинаковых временах
    o_s7 = Offer(
        site="aviasales",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7 Airlines",
    )
    o_afl = Offer(
        site="kupibilet",
        price=2800,
        depart_time="10:00",
        arrive_time="12:00",
        airline="Аэрофлот",
    )
    assert not same_flight(o_s7, o_afl)
    merged_diff = merge_same_flights([o_s7, o_afl])
    assert len(merged_diff) == 2
    assert merged_diff[0].alts is None
    assert merged_diff[1].alts is None

    # 2. Одна авиакомпания пустая (None или "") → склеиваются
    o_empty1 = Offer(
        site="tutu",
        price=2500,
        depart_time="10:00",
        arrive_time="12:00",
        airline=None,
    )
    assert same_flight(o_s7, o_empty1)
    merged_empty1 = merge_same_flights([o_s7, o_empty1])
    assert len(merged_empty1) == 1
    assert merged_empty1[0].site == "tutu"
    assert merged_empty1[0].price == 2500
    assert merged_empty1[0].alts is not None
    assert len(merged_empty1[0].alts) == 1
    assert merged_empty1[0].alts[0].site == "aviasales"

    o_empty2 = Offer(
        site="onetwotrip",
        price=2600,
        depart_time="10:00",
        arrive_time="12:00",
        airline="",
    )
    assert same_flight(o_s7, o_empty2)
    merged_empty2 = merge_same_flights([o_s7, o_empty2])
    assert len(merged_empty2) == 1

    # 3. Нормализация авиакомпании (регистр, знаки препинания, обрезка до первого слова)
    o_norm1 = Offer(
        site="aviasales",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="«Уральские авиалинии»",
    )
    o_norm2 = Offer(
        site="kupibilet",
        price=2900,
        depart_time="10:00",
        arrive_time="12:00",
        airline="уральские",
    )
    assert normalize_airline("«Уральские авиалинии»") == "уральские"
    assert same_flight(o_norm1, o_norm2)
    assert len(merge_same_flights([o_norm1, o_norm2])) == 1


def test_roundtrip_legs_matching():
    """(г) рейс «туда-обратно»: склеивается только если совпадают оба плеча."""
    back_leg = Leg(
        depart_time="18:00",
        arrive_time="20:00",
        transfers=0,
        airline="S7",
    )
    back_leg_diff = Leg(
        depart_time="19:00",
        arrive_time="21:00",
        transfers=0,
        airline="S7",
    )

    o_rt1 = Offer(
        site="aviasales",
        price=6000,
        depart_time="10:00",
        arrive_time="12:00",
        transfers=0,
        airline="S7",
        back=back_leg,
    )
    o_rt2 = Offer(
        site="kupibilet",
        price=5500,
        depart_time="10:00",
        arrive_time="12:00",
        transfers=0,
        airline="S7",
        back=back_leg,
    )
    o_rt_diff_back = Offer(
        site="tutu",
        price=5400,
        depart_time="10:00",
        arrive_time="12:00",
        transfers=0,
        airline="S7",
        back=back_leg_diff,
    )
    o_oneway = Offer(
        site="onetwotrip",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        transfers=0,
        airline="S7",
        back=None,
    )

    # Оба плеча совпадают → склеиваются
    assert same_flight(o_rt1, o_rt2)
    merged_rt = merge_same_flights([o_rt1, o_rt2])
    assert len(merged_rt) == 1
    assert merged_rt[0].site == "kupibilet"
    assert merged_rt[0].alts is not None
    assert len(merged_rt[0].alts) == 1

    # Обратное плечо отличается → не склеиваются
    assert not same_flight(o_rt1, o_rt_diff_back)
    assert len(merge_same_flights([o_rt1, o_rt_diff_back])) == 2

    # Одно плечо туда-обратно, другое в одну сторону → не склеиваются
    assert not same_flight(o_rt1, o_oneway)
    assert len(merge_same_flights([o_rt1, o_oneway])) == 2


def test_same_site_not_merged():
    """(д) одного сайта не склеивает."""
    o1 = Offer(
        site="aviasales",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7",
    )
    o2 = Offer(
        site="aviasales",
        price=4000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7",
    )

    merged = merge_same_flights([o1, o2])
    assert len(merged) == 2
    assert merged[0].price == 3000
    assert merged[0].alts is None
    assert merged[1].price == 4000
    assert merged[1].alts is None


def test_build_compact_report_pool_no_dups_and_card_text():
    """(е) build_compact_report на трёх результатах: pool без дублей, в тексте карточки есть строка «также»."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 16))
    o_avia = Offer(
        site="aviasales",
        price=2200,
        depart_time="10:15",
        arrive_time="11:45",
        airline="Аэрофлот",
        url="https://aviasales.ru/ticket",
    )
    o_tutu = Offer(
        site="tutu",
        price=2169,
        depart_time="10:15",
        arrive_time="11:45",
        airline="Аэрофлот",
        url="https://tutu.ru/ticket",
    )
    o_ya = Offer(
        site="yandex_travel",
        price=2176,
        depart_time="10:15",
        arrive_time="11:45",
        airline="Аэрофлот",
        url="https://travel.yandex.ru/ticket",
    )

    results = [
        SiteResult(site="aviasales", ok=True, offers=[o_avia]),
        SiteResult(site="tutu", ok=True, offers=[o_tutu]),
        SiteResult(site="yandex_travel", ok=True, offers=[o_ya]),
    ]

    criteria = SearchCriteria()
    chunks, flat, pool = build_compact_report(q, results, criteria)

    # Pool содержит только 1 склеенный билет без дублей
    assert len(pool) == 1
    assert len(flat) == 1
    rep = pool[0]
    assert rep.site == "tutu"
    assert rep.price == 2169
    assert rep.alts is not None
    assert len(rep.alts) == 2

    # В тексте отчёта есть строка с альтернативными ценами
    full_text = "\n".join(chunks)
    assert "такой же" in full_text
    assert "💰 такой же:" in full_text
    assert "Яндекс" in full_text
    assert "Aviasales" in full_text
    assert "2 176" in full_text.replace("\xa0", " ")
    assert "2 200" in full_text.replace("\xa0", " ")


def test_original_offers_not_mutated():
    """(ж) исходные Offer не мутируются."""
    o1 = Offer(
        site="aviasales",
        price=3000,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7",
        alts=None,
    )
    o2 = Offer(
        site="kupibilet",
        price=2500,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7",
        alts=None,
    )

    merged = merge_same_flights([o1, o2])
    assert len(merged) == 1

    # Исходные объекты не должны измениться
    assert o1.alts is None
    assert o2.alts is None
    # Результат — новый объект Offer
    assert merged[0] is not o1
    assert merged[0] is not o2
    assert merged[0].alts is not None


def test_missing_departure_time_not_merged():
    """(з) билет без времени вылета остаётся отдельным."""
    o_no_time = Offer(
        site="aviasales",
        price=2000,
        depart_time=None,
        depart_at=None,
        airline="S7",
    )
    o_with_time = Offer(
        site="kupibilet",
        price=2500,
        depart_time="10:00",
        arrive_time="12:00",
        airline="S7",
    )

    assert flight_key(o_no_time) is None
    assert flight_key(o_with_time) is not None
    assert not same_flight(o_no_time, o_with_time)

    merged = merge_same_flights([o_no_time, o_with_time])
    assert len(merged) == 2
    assert merged[0].site == "aviasales"
    assert merged[0].alts is None
    assert merged[1].site == "kupibilet"
    assert merged[1].alts is None


def test_different_currencies_not_merged():
    """Билеты с разными валютами не склеиваются."""
    o1 = Offer(
        site="aviasales",
        price=100,
        currency="USD",
        depart_time="10:00",
        arrive_time="12:00",
    )
    o2 = Offer(
        site="kupibilet",
        price=9000,
        currency="RUB",
        depart_time="10:00",
        arrive_time="12:00",
    )
    assert flight_key(o1) != flight_key(o2)
    assert not same_flight(o1, o2)
    merged = merge_same_flights([o1, o2])
    assert len(merged) == 2
    assert merged[0].alts is None
    assert merged[1].alts is None


def test_offer_card_alts_formatting():
    """Проверка форматирования карточки: до 3 штук, ссылки, экранирование, отсутствие строки при пустом alts."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 16))

    # Билет без alts -> строки «также» нет
    o_single = Offer(
        site="aviasales",
        price=2000,
        depart_time="10:00",
        arrive_time="12:00",
        alts=None,
    )
    card_single = offer_card(o_single, q, number=1)
    card_text_single = "\n".join(card_single)
    assert "такой же" not in card_text_single

    # Билет с 4 alts -> показываются только первые 3, со ссылками
    o_with_alts = Offer(
        site="aviasales",
        price=2000,
        depart_time="10:00",
        arrive_time="12:00",
        alts=[
            OfferAlt(site="tutu", price=2100, currency="RUB", url="https://tutu.ru/ticket"),
            OfferAlt(site="yandex_travel", price=2150, currency="RUB", url=None),
            OfferAlt(site="kupibilet", price=2200, currency="RUB", url="https://kupi.ru/t"),
            OfferAlt(site="onetwotrip", price=2300, currency="RUB", url="https://ott.ru/t"),
        ],
    )
    card = offer_card(o_with_alts, q, number=1)
    card_text = "\n".join(card)
    assert "💰 такой же:" in card_text
    assert '<a href="https://tutu.ru/ticket">Туту</a> 2 100 ₽' in card_text.replace("\xa0", " ")
    assert "Яндекс Путешествия 2 150 ₽" in card_text.replace("\xa0", " ")
    assert '<a href="https://kupi.ru/t">Купибилет</a> 2 200 ₽' in card_text.replace("\xa0", " ")
    assert "OneTwoTrip" not in card_text  # 4-й не должен попасть (до 3 штук)
