import re
from datetime import date, datetime
from html import escape

from avia.formatting import build_report, format_details, format_report
from avia.models import Leg, Offer, Query, SiteResult


def _check_html_tags_balanced(text: str) -> None:
    """Проверяет, что базовые HTML-теги сбалансированы в сообщении."""
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")
    # Теги ссылок могут иметь атрибуты, например <a href="...">
    assert len(re.findall(r"<a\b", text)) == text.count("</a>")


def test_continuous_numbering_across_sites():
    """Сквозная нумерация вариантов (1., 2., 3...) через все сайты."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o1 = Offer("site_a", 10000, airline="A1", depart_time="10:00")
    o2 = Offer("site_a", 11000, airline="A2", depart_time="11:00")
    o3 = Offer("site_b", 12000, airline="B1", depart_time="12:00")
    o4 = Offer("site_b", 13000, airline="B2", depart_time="13:00")

    res = [
        SiteResult("site_a", True, [o1, o2]),
        SiteResult("site_b", True, [o3, o4]),
    ]

    parts, flat = build_report(q, res, top_n=5)
    report = "\n".join(parts)

    assert "<b>1.</b> <b>10 000 ₽</b>" in report
    assert "<b>2.</b> <b>11 000 ₽</b>" in report
    assert "<b>3.</b> <b>12 000 ₽</b>" in report
    assert "<b>4.</b> <b>13 000 ₽</b>" in report
    assert flat == [o1, o2, o3, o4]
    assert "Напишите номер билета, чтобы увидеть подробности" in report


def test_long_report_chunking_and_balanced_tags():
    """Нарезка длинного отчёта (200 вариантов): все части ≤4096, теги сбалансированы, нумерация непрерывна."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    offers = [
        Offer(
            site="big_site",
            price=10000 + i * 10,
            airline=f"Airline {i % 5}",
            depart_time=f"{i % 24:02d}:00",
            arrive_time=f"{(i + 4) % 24:02d}:00",
            duration="4 ч 00 мин",
            transfers=i % 2,
            baggage="с багажом",
        )
        for i in range(200)
    ]
    res = [SiteResult("big_site", True, offers, elapsed=1.5)]

    parts, flat = build_report(q, res, top_n=200)

    # Проверяем, что отчёт разрезан на несколько частей
    assert len(parts) > 1
    assert len(flat) == 200

    # Проверяем лимит длины и сбалансированность тегов в каждой части
    for part in parts:
        assert len(part) <= 4096
        assert len(part) <= 4000
        _check_html_tags_balanced(part)

    # Проверяем непрерывность нумерации от 1 до 200
    full_text = "\n".join(parts)
    for i in range(1, 201):
        assert f"<b>{i}.</b>" in full_text

    # Проверяем наличие заголовка продолжения
    assert "<b>big_site</b> (продолжение)" in full_text


def test_format_details_single_leg_and_script_escaping():
    """Карточка билета с одним плечом и корректным экранированием HTML/<script>."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(
        site="<script>site</script>",
        price=15000,
        airline="Air <script>Astana</script>",
        depart_time="10:00",
        arrive_time="14:30",
        duration="4 ч 30 мин",
        transfers=1,
        flight_numbers=["KC <script>101</script>"],
        aircraft="Airbus <script>A320</script>",
        layovers=["Астана, 2 ч <script>10 мин</script>"],
        baggage="с багажом <script>23 кг</script>",
        carry_on="10 кг <script>ручная</script>",
        url="https://avia.example.com/buy?id=123&foo=<script>",
        fetched_at=1700000000.0,
    )

    card = format_details(o, q)

    # <script> нигде не должно остаться сырым
    assert "<script>" not in card
    assert "&lt;script&gt;" in card

    # Проверка основных полей
    assert "Москва → Алматы" in card
    assert "10 дек, 10:00" in card
    assert "10 дек, 14:30" in card
    assert "Air &lt;script&gt;Astana&lt;/script&gt;" in card
    assert "Рейс:  KC &lt;script&gt;101&lt;/script&gt;" in card
    assert "Самолёт:  Airbus &lt;script&gt;A320&lt;/script&gt;" in card
    assert "В пути:  4 ч 30 мин" in card
    assert "Пересадки:  Астана, 2 ч &lt;script&gt;10 мин&lt;/script&gt;" in card
    assert "Багаж: с багажом &lt;script&gt;23 кг&lt;/script&gt;" in card
    assert "Ручная кладь: 10 кг &lt;script&gt;ручная&lt;/script&gt;" in card
    assert "15 000 ₽" in card
    assert "Сайт: &lt;script&gt;site&lt;/script&gt;" in card
    assert 'href="https://avia.example.com/buy?id=123&amp;foo=&lt;script&gt;"' in card
    assert "Цена снята в " in card


def test_format_details_two_legs():
    """Карточка билета с двумя плечами (туда и обратно)."""
    q = Query("MOW", "ALA", date(2026, 12, 10), return_date=date(2026, 12, 20))
    back_leg = Leg(
        airline="Air Astana",
        depart_time="18:00",
        arrive_time="22:30",
        duration="4 ч 30 мин",
        transfers=0,
        flight_numbers=["KC 102"],
        aircraft="Boeing 737",
    )
    o = Offer(
        site="tutu",
        price=32000,
        airline="Air Astana",
        depart_time="10:00",
        arrive_time="14:30",
        duration="4 ч 30 мин",
        transfers=0,
        back=back_leg,
        fetched_at=1700000000.0,
    )

    card = format_details(o, q)

    assert "Москва → Алматы" in card
    assert "Обратно: Алматы → Москва" in card
    assert "Алматы → Москва" in card
    assert "20 дек, 18:00" in card
    assert "20 дек, 22:30" in card
    assert "Рейс:  KC 102" in card
    assert "Самолёт:  Boeing 737" in card
    assert "32 000 ₽" in card
    assert "Сайт: tutu"


def test_header_baggage_mode_and_variant_notice():
    """Шапка отчёта при фильтре багажа и пометка 'багаж не указан' в строке."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o_with = Offer("site1", 10000, airline="A1", depart_time="10:00", baggage="с багажом")
    o_unknown = Offer("site1", 12000, airline="A2", depart_time="12:00", baggage=None)

    report_with = format_report(q, [SiteResult("site1", True, [o_with, o_unknown])], baggage_mode="with", dropped=2)

    assert "только с багажом · скрыто: 2" in report_with
    assert "багаж не указан" in report_with

    # В режиме any пометки в шапке и строке варианта нет
    report_any = format_report(q, [SiteResult("site1", True, [o_unknown])], baggage_mode="any")
    assert "скрыто:" not in report_any
    assert "багаж не указан" not in report_any


def test_header_return_date():
    """Шапка отчёта при наличии обратной даты."""
    q = Query("MOW", "ALA", date(2026, 12, 10), return_date=date(2026, 12, 20))
    report = format_report(q, [SiteResult("site1", True, [])])

    assert "10 дек – 20 дек" in report
    assert "цена за оба плеча" in report
