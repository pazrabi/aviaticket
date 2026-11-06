from __future__ import annotations

from datetime import date

from avia.config import Settings
from avia.filters import (
    is_noticeably_better,
    pick_site_top,
    total_duration_minutes,
)
from avia.formatting import build_report, format_middle_diff
from avia.models import Offer, Query, SiteResult


def test_pick_site_top_counts():
    """(а) набор из 10 офферов даёт ровно 5 без повторов, набор из 3 даёт 3."""
    # 10 офферов одного сайта
    offers_10 = [
        Offer(
            site="s1",
            price=10000 + i * 500,
            duration=f"{10 - (i % 5)} ч",
            transfers=i % 2,
            baggage="без багажа" if i % 2 == 0 else "с багажом",
            offer_id=f"off_{i}",
        )
        for i in range(10)
    ]
    top_5 = pick_site_top(offers_10)
    assert len(top_5) == 5
    # Проверка отсутствия дубликатов
    assert len({id(o) for o in top_5}) == 5
    assert len({o.offer_id for o in top_5}) == 5
    # Результат отсортирован по цене
    assert [o.price for o in top_5] == sorted(o.price for o in top_5)

    # 3 оффера одного сайта -> ровно 3
    offers_3 = [
        Offer(site="s1", price=10000, duration="5 ч", offer_id="o1"),
        Offer(site="s1", price=11000, duration="4 ч", offer_id="o2"),
        Offer(site="s1", price=12000, duration="3 ч", offer_id="o3"),
    ]
    top_3 = pick_site_top(offers_3)
    assert len(top_3) == 3
    assert [o.offer_id for o in top_3] == ["o1", "o2", "o3"]


def test_site_top_middle_by_time():
    """(б) середина по времени: дешёвый 10 000 ₽ и 14 ч, билет 11 500 ₽ и 5 ч — середина, билет 10 500 ₽ и 13 ч — нет."""
    cheap = Offer(site="s1", price=10000, duration="14 ч", baggage="без багажа", offer_id="c")
    o_mid = Offer(site="s1", price=11500, duration="5 ч", baggage="без багажа", offer_id="m")
    o_not_mid = Offer(site="s1", price=10500, duration="13 ч", baggage="без багажа", offer_id="nm")

    # Проверка функции заметного улучшения
    # o_mid: выигрыш 9 ч (540 мин) >= 120 мин и >= 25% от 14 ч (210 мин), цена +15% <= 30% -> середина
    assert is_noticeably_better(o_mid, cheap) is True
    # o_not_mid: выигрыш 1 ч (60 мин) < 120 мин -> не середина
    assert is_noticeably_better(o_not_mid, cheap) is False

    # В pick_site_top: cheap (1-й дешёвый), o_not_mid (2-й дешёвый), o_mid (середина)
    extra_slow = Offer(site="s1", price=12000, duration="15 ч", baggage="без багажа", offer_id="es")
    extra_slow2 = Offer(site="s1", price=13000, duration="16 ч", baggage="без багажа", offer_id="es2")
    offers = [cheap, o_not_mid, o_mid, extra_slow, extra_slow2]

    chosen = pick_site_top(offers)
    assert len(chosen) == 5
    # o_mid должен быть выбран как «середина»
    mid_picked = next(o for o in chosen if o.offer_id == "m")
    assert getattr(mid_picked, "_site_top_role", None) == "balance"
    # o_not_mid выбран как 2-й дешёвый, не середина
    nm_picked = next(o for o in chosen if o.offer_id == "nm")
    assert getattr(nm_picked, "_site_top_role", None) == "cheap"


def test_site_top_middle_by_baggage():
    """(в) середина по багажу: дешёвый без багажа, билет на 900 ₽ дороже с багажом — середина с пометкой «с багажом, +900 ₽» (режим any); в режиме 'with'/'without' условие багажа не работает."""
    cheap = Offer(site="s1", price=10000, duration="5 ч", baggage="без багажа", offer_id="c")
    cheap2 = Offer(site="s1", price=10200, duration="5 ч", baggage="без багажа", offer_id="c2")
    o_bag = Offer(site="s1", price=10900, duration="5 ч", baggage="с багажом", offer_id="b")

    # В режиме any:
    assert is_noticeably_better(o_bag, cheap, baggage_mode="any") is True
    diff_note = format_middle_diff(cheap, o_bag, baggage_mode="any")
    assert diff_note == "(с багажом, +900 ₽)"

    # В режиме with/without условие по багажу не работает (время одинаковое, выигрыша нет)
    assert is_noticeably_better(o_bag, cheap, baggage_mode="with") is False
    assert is_noticeably_better(o_bag, cheap, baggage_mode="without") is False
    assert format_middle_diff(cheap, o_bag, baggage_mode="with") == ""
    assert format_middle_diff(cheap, o_bag, baggage_mode="without") == ""

    # Проверка роли в pick_site_top в режиме any
    res_any = pick_site_top([cheap, cheap2, o_bag], baggage_mode="any")
    bag_picked = next(o for o in res_any if o.offer_id == "b")
    assert getattr(bag_picked, "_site_top_role", None) == "balance"

    # В режиме with билет с багажом не получает роль balance
    res_with = pick_site_top([cheap, cheap2, o_bag], baggage_mode="with")
    bag_picked_with = next(o for o in res_with if o.offer_id == "b")
    assert getattr(bag_picked_with, "_site_top_role", None) != "balance"


def test_site_top_40pct_too_expensive():
    """(г) билет на 40% дороже в середину не попадает, даже если намного быстрее."""
    cheap = Offer(site="s1", price=10000, duration="14 ч", baggage="без багажа", offer_id="c1")
    cheap2 = Offer(site="s1", price=10500, duration="14 ч", baggage="без багажа", offer_id="c2")
    # 14 000 ₽ — на 40% дороже 10 000 ₽ (> 30% порога BALANCE_MAX_PREMIUM_PCT)
    fast_expensive = Offer(site="s1", price=14000, duration="1 ч", baggage="без багажа", offer_id="fe")
    other = Offer(site="s1", price=15000, duration="12 ч", baggage="без багажа", offer_id="o")

    # В pick_site_top fast_expensive не может быть выбран как "balance"
    top = pick_site_top([cheap, cheap2, fast_expensive, other])
    fe_in_top = next((o for o in top if o.offer_id == "fe"), None)
    assert fe_in_top is not None
    # Он может быть выбран как fast (самый быстрый), но НЕ balance
    assert getattr(fe_in_top, "_site_top_role", None) != "balance"
    assert getattr(fe_in_top, "_site_top_role", None) == "fast"


def test_site_top_transfers_removed():
    """(д) условие «меньше пересадок» больше не делает билет серединой."""
    cheap = Offer(site="s1", price=10000, duration="5 ч", transfers=2, baggage="без багажа", offer_id="c1")
    cheap2 = Offer(site="s1", price=10200, duration="5 ч", transfers=2, baggage="без багажа", offer_id="c2")
    direct = Offer(site="s1", price=11000, duration="5 ч", transfers=0, baggage="без багажа", offer_id="dir")

    # Выигрыш только в пересадках (время и багаж одинаковые) -> не заметно лучше
    assert is_noticeably_better(direct, cheap) is False
    assert format_middle_diff(cheap, direct) == ""

    top = pick_site_top([cheap, cheap2, direct])
    dir_picked = next(o for o in top if o.offer_id == "dir")
    assert getattr(dir_picked, "_site_top_role", None) != "balance"


def test_build_report_integration():
    """Интеграционный тест build_report с пометками ⚖️ и ⚡."""
    q = Query("MOW", "LED", date(2026, 12, 10))
    cheap1 = Offer("s1", 10000, duration="14 ч", baggage="без багажа", airline="SU", offer_id="c1")
    cheap2 = Offer("s1", 10500, duration="14 ч", baggage="без багажа", airline="S7", offer_id="c2")
    mid_bag = Offer("s1", 10900, duration="14 ч", baggage="с багажом", airline="U6", offer_id="mb")
    mid_time = Offer("s1", 11500, duration="5 ч", baggage="без багажа", airline="DP", offer_id="mt")
    fastest = Offer("s1", 12500, duration="2 ч", baggage="без багажа", airline="UT", offer_id="fst")
    extra = Offer("s1", 13000, duration="10 ч", baggage="без багажа", airline="A4", offer_id="ex")

    res = [SiteResult("s1", True, [cheap1, cheap2, mid_bag, mid_time, fastest, extra])]
    parts, flat = build_report(q, res)
    assert len(flat) == 5
    report = "\n".join(parts)

    # Нумерация 1..5 непрерывна
    for i in range(1, 6):
        assert f"<b>{i}.</b>" in report

    # У золотой середины стоит ⚖️ и причина
    assert "⚖️ с багажом, +900 ₽" in report
    assert "⚖️ на 9 ч быстрее, +1 500 ₽" in report
    # У самого быстрого стоит ⚡
    assert "⚡" in report

    # Все билеты в flat упорядочены по возрастанию цены
    assert [o.price for o in flat] == sorted(o.price for o in flat)


def test_custom_settings():
    """Проверка кастомных настроек Settings для порогов и отбора."""
    s = Settings(
        sites=[],
        balance_max_premium_pct=50.0,
        balance_min_saving_minutes=60,
        balance_min_saving_share=0.10,
        site_top_cheap=1,
        site_top_balance=1,
        site_top_fast=1,
    )
    cheap = Offer("s1", 10000, duration="5 ч", offer_id="c")
    # Дороже на 40% (14 000 ₽), выигрыш 70 мин:
    # При дефолтных порогах (30% и 120 мин) не прошёл бы, но с s проходит
    cand = Offer("s1", 14000, duration="3 ч 50 м", offer_id="mid")
    top = pick_site_top([cheap, cand], settings=s)
    assert len(top) == 2
    assert getattr(top[1], "_site_top_role", None) == "balance"
