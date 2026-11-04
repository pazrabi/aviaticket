"""Тесты форматирования отчёта и карточек для режима туда-обратно и названий городов."""
from datetime import date
from html import escape
import re

from avia.formatting import build_report, format_details, format_report, money, offer_line
from avia.models import Leg, Offer, Query, SiteResult


def _check_html_tags_balanced(text: str) -> None:
    assert "<!--" not in text
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")
    assert len(re.findall(r"<a\b", text)) == text.count("</a>")


def test_header_city_names_and_unknown_code():
    """Шапка отчёта и карточки показывает названия городов, неизвестный код остаётся кодом, всё через escape."""
    # 1. Известные коды (KUF -> Самара, EVN -> Ереван) в одностороннем запросе
    q_oneway = Query("KUF", "EVN", date(2026, 12, 16))
    rep_oneway = format_report(q_oneway, [SiteResult("site", True, [])])
    assert "✈️ <b>Самара → Ереван</b>" in rep_oneway
    assert "16 дек" in rep_oneway

    # 2. Известные коды в запросе туда-обратно
    q_rt = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    rep_rt = format_report(q_rt, [SiteResult("site", True, [])])
    assert "✈️ <b>Самара → Ереван</b>" in rep_rt
    assert "16 дек – 20 дек" in rep_rt
    assert "цена за оба плеча" in rep_rt

    # 3. Неизвестный код (XYZ остаётся XYZ, EVN -> Ереван)
    q_unk = Query("XYZ", "EVN", date(2026, 12, 16))
    rep_unk = format_report(q_unk, [SiteResult("site", True, [])])
    assert "✈️ <b>XYZ → Ереван</b>" in rep_unk

    # 4. Оба кода неизвестны
    q_unk2 = Query("ABC", "DEF", date(2026, 12, 16))
    rep_unk2 = format_report(q_unk2, [SiteResult("site", True, [])])
    assert "✈️ <b>ABC → DEF</b>" in rep_unk2

    # 5. Карточка (format_details) с известными городами (оба плеча)
    o_rt = Offer(
        site="tutu",
        price=42898,
        airline="FLYONE Armenia",
        depart_time="10:15",
        arrive_time="13:30",
        back=Leg(airline="FLYONE Armenia", depart_time="08:00", arrive_time="11:10"),
    )
    card_rt = format_details(o_rt, q_rt)
    assert "Туда: Самара → Ереван" in card_rt
    assert "Обратно: Ереван → Самара" in card_rt

    # 6. Карточка с неизвестным кодом
    card_unk = format_details(o_rt, q_unk)
    assert "Туда: XYZ → Ереван" in card_unk

    # 7. Экранирование спецсимволов в кодах
    q_esc = Query("<orig>", "<dest>", date(2026, 12, 16))
    rep_esc = format_report(q_esc, [SiteResult("site", True, [])])
    assert "&lt;orig&gt; → &lt;dest&gt;" in rep_esc
    assert "<orig>" not in rep_esc


def test_roundtrip_offer_line_two_legs_different_airlines():
    """Строка туда-обратно с двумя плечами, разными авиакомпаниями и дедупликацией."""
    # Разные авиакомпании: «A / B»
    o_diff = Offer(
        site="aviasales",
        price=42898,
        airline="FLYONE Armenia",
        depart_time="10:15",
        arrive_time="13:30",
        transfers=0,
        baggage="без багажа",
        back=Leg(
            airline="Pegasus Airlines",
            depart_time="08:00",
            arrive_time="11:10",
            transfers=1,
        ),
    )
    line_diff = offer_line(o_diff)
    assert "<b>42 898 ₽</b>" in line_diff
    assert "FLYONE Armenia / Pegasus Airlines" in line_diff
    assert "туда 10:15→13:30 (прямой)" in line_diff
    assert "обратно 08:00→11:10 (1 пересадка)" in line_diff
    assert "без багажа" in line_diff

    # Одинаковые авиакомпании: одна авиакомпания
    o_same = Offer(
        site="aviasales",
        price=42898,
        airline="FLYONE Armenia",
        depart_time="10:15",
        arrive_time="13:30",
        transfers=0,
        baggage="без багажа",
        back=Leg(
            airline="FLYONE Armenia",
            depart_time="08:00",
            arrive_time="11:10",
            transfers=1,
        ),
    )
    line_same = offer_line(o_same)
    assert "FLYONE Armenia / FLYONE Armenia" not in line_same
    expected_same = (
        "<b>42 898 ₽</b> · FLYONE Armenia · туда 10:15→13:30 (прямой) · "
        "обратно 08:00→11:10 (1 пересадка) · без багажа"
    )
    assert line_same == expected_same

    # Дедупликация строк в пределах сайта работает по новой строке
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    dup_offers = [o_same, o_same, o_diff]
    res = SiteResult("aviasales", True, dup_offers)
    text = format_report(q, [res], top_n=5)
    # o_same должен встретиться в списке ровно 1 раз (плюс строка «Дешевле всего»)
    assert text.count("10:15 → 13:30") == 2
    assert "<b>1.</b>" in text
    assert "<b>2.</b>" in text
    assert "<b>3.</b>" not in text


def test_roundtrip_offer_line_duration_omitted_when_longer_than_160():
    """Длительность каждого плеча опускается, если строка выходит длиннее ~160 символов."""
    # 1. Короткая строка: длительность остаётся
    o_short = Offer(
        site="s",
        price=20000,
        airline="S7",
        depart_time="10:00",
        arrive_time="12:00",
        duration="2 ч",
        transfers=0,
        baggage="без багажа",
        back=Leg(
            airline="S7",
            depart_time="14:00",
            arrive_time="16:00",
            duration="2 ч",
            transfers=0,
        ),
    )
    line_short = offer_line(o_short)
    assert len(line_short) <= 160
    assert "туда 10:00→12:00, 2 ч (прямой)" in line_short
    assert "обратно 14:00→16:00, 2 ч (прямой)" in line_short

    # 2. Длинная строка (>160 символов): длительность опускается, времена и пересадки остаются
    o_long = Offer(
        site="s",
        price=99999,
        airline="Scandinavian Airlines System",
        depart_time="10:00",
        arrive_time="15:30",
        duration="15 ч 30 мин длительность плеча",
        transfers=2,
        baggage="с багажом 23 кг (дополнительный специальный сбор включён)",
        back=Leg(
            airline="Turkish Airlines International",
            depart_time="18:00",
            arrive_time="23:45",
            duration="16 ч 45 мин длительность плеча",
            transfers=3,
        ),
    )
    line_long = offer_line(o_long)
    # Длительность должна быть опущена
    assert "длительность плеча" not in line_long
    # Времена и пересадки должны остаться
    assert "туда 10:00→15:30 (пересадок: 2)" in line_long
    assert "обратно 18:00→23:45 (пересадок: 3)" in line_long


def test_roundtrip_offer_line_empty_leg_brief():
    """Если у плеча нет данных о времени, длительности и пересадках, 'туда'/'обратно' не показываются."""
    o = Offer(
        site="site",
        price=10000,
        airline="Airline",
        back=Leg(),
    )
    line = offer_line(o)
    assert line == "<b>10 000 ₽</b> · Airline"
    assert "туда" not in line
    assert "обратно" not in line


def test_oneway_offer_line_unchanged():
    """Для односторонних офферов (o.back is None) строка остаётся ровно прежней (регрессия!)."""
    o1 = Offer(
        site="x",
        price=16403,
        airline="Nordwind",
        depart_time="22:50",
        arrive_time="18:25",
        duration="4 ч 30 мин",
        transfers=1,
        baggage="без багажа",
    )
    assert offer_line(o1) == "<b>16 403 ₽</b> · Nordwind · 22:50→18:25 · 4 ч 30 мин · пересадок: 1 · без багажа"

    o0 = Offer(
        site="x",
        price=10000,
        airline="Aeroflot",
        depart_time="10:00",
        arrive_time="12:00",
        transfers=0,
    )
    assert offer_line(o0) == "<b>10 000 ₽</b> · Aeroflot · 10:00→12:00 · прямой"

    # Режим baggage_mode != "any" и неизвестный багаж
    o_unk = Offer(site="x", price=12000, airline="A2", depart_time="12:00", baggage=None)
    assert offer_line(o_unk, baggage_mode="with") == "<b>12 000 ₽</b> · A2 · вылет 12:00 · багаж не указан"
    assert offer_line(o_unk, baggage_mode="any") == "<b>12 000 ₽</b> · A2 · вылет 12:00"


def test_escape_in_back_leg_airline():
    """escape('<b>') в авиакомпании обратного плеча."""
    o = Offer(
        site="site",
        price=25000,
        airline="NormalAir",
        depart_time="10:00",
        arrive_time="12:00",
        transfers=0,
        back=Leg(
            airline="Evil<b>Carrier</b>",
            depart_time="14:00",
            arrive_time="16:00",
            transfers=0,
        ),
    )
    line = offer_line(o)
    assert "<b>Carrier</b>" not in line
    assert "&lt;b&gt;Carrier&lt;/b&gt;" in line
    assert "NormalAir / Evil&lt;b&gt;Carrier&lt;/b&gt;" in line

    # Также проверяем экранирование в format_details
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    card = format_details(o, q)
    assert "<b>Carrier</b>" not in card
    assert "&lt;b&gt;Carrier&lt;/b&gt;" in card
    _check_html_tags_balanced(card)


def test_build_report_chunks_length_and_balanced_tags():
    """Длина всех частей build_report по-прежнему ≤ 4096 (и ≤ 4000), теги сбалансированы."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    offers = [
        Offer(
            site="big_site",
            price=20000 + i * 50,
            airline=f"Airline A{i % 3}",
            depart_time=f"{i % 24:02d}:00",
            arrive_time=f"{(i + 3) % 24:02d}:00",
            duration="3 ч",
            transfers=i % 2,
            baggage="без багажа",
            back=Leg(
                airline=f"Airline B{i % 4}",
                depart_time=f"{(i + 6) % 24:02d}:00",
                arrive_time=f"{(i + 9) % 24:02d}:00",
                duration="3 ч",
                transfers=(i + 1) % 2,
            ),
        )
        for i in range(120)
    ]
    res = [SiteResult("big_site", True, offers, elapsed=2.0)]

    parts, flat = build_report(q, res, top_n=120)

    assert len(parts) > 1
    assert len(flat) == 120

    for part in parts:
        assert len(part) <= 4096
        assert len(part) <= 4000
        _check_html_tags_balanced(part)
