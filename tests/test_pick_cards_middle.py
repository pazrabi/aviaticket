from __future__ import annotations

from datetime import date

from avia.criteria import SearchCriteria
from avia.filters import (
    BALANCE_MAX_PREMIUM_PCT,
    BALANCE_MIN_SAVING_MINUTES,
    BALANCE_MIN_SAVING_SHARE,
    MIDDLE_DEFAULT_HOUR_COST,
    MIDDLE_MAX_EXTRA_RATIO,
    MIDDLE_MIN_TIME_GAIN_MINUTES,
    MIDDLE_MIN_TIME_GAIN_RATIO,
    SITE_TOP_BALANCE,
    SITE_TOP_CHEAP,
    SITE_TOP_FAST,
    pick_cards,
)
from avia.formatting import build_compact_report, format_middle_diff, offer_card
from avia.models import Leg, Offer, Query, SiteResult


def test_constants_present():
    """Константы порогов экспортируются в filters.py с требуемыми значениями по правилу владельца."""
    assert BALANCE_MAX_PREMIUM_PCT == 30.0
    assert BALANCE_MIN_SAVING_MINUTES == 120
    assert BALANCE_MIN_SAVING_SHARE == 0.25
    assert SITE_TOP_CHEAP == 2
    assert SITE_TOP_BALANCE == 2
    assert SITE_TOP_FAST == 1
    # Для обратной совместимости
    assert MIDDLE_MAX_EXTRA_RATIO == 0.30
    assert MIDDLE_MIN_TIME_GAIN_RATIO == 0.25
    assert MIDDLE_MIN_TIME_GAIN_MINUTES == 120
    assert MIDDLE_DEFAULT_HOUR_COST == 300


def test_pick_cards_middle_added():
    """(а) дешёвый 3000 ₽ 5 ч с пересадкой, «середина» 3300 ₽ 3 ч прямой (на 2 ч быстрее) → карточка добавляется."""
    cheap = Offer(site="s", price=3000, duration="5 ч", transfers=1, offer_id="cheap")
    middle = Offer(site="s", price=3300, duration="3 ч", transfers=0, offer_id="middle")
    fast = Offer(site="s", price=5000, duration="2 ч", transfers=0, offer_id="fast")

    cards = pick_cards([cheap, fast, middle], SearchCriteria())
    titles = [t for t, _ in cards]
    assert titles == ["Самый дешёвый", "Самый быстрый", "Золотая середина"]
    assert cards[2][1] is middle


def test_pick_cards_middle_too_expensive():
    """(б) кандидат дороже +30% (порог BALANCE_MAX_PREMIUM_PCT=30) → нет."""
    cheap = Offer(site="s", price=3000, duration="5 ч", transfers=1, offer_id="cheap")
    # 3000 * 1.30 = 3900; 4000 > 3900
    cand = Offer(site="s", price=4000, duration="3 ч", transfers=0, offer_id="cand")
    fast = Offer(site="s", price=5000, duration="2 ч", transfers=0, offer_id="fast")

    cards = pick_cards([cheap, fast, cand], SearchCriteria())
    assert not any(t == "Золотая середина" for t, _ in cards)


def test_pick_cards_middle_not_noticeably_better():
    """(в) кандидат не лучше по удобству → нет."""
    # Время: 5 ч (300 м) -> 4 ч 50 м (290 м) — выигрыш 10 м (< 120 м и < 25%)
    # Багаж: без багажа vs без багажа (не лучше)
    cheap = Offer(
        site="s", price=3000, duration="5 ч", transfers=1, baggage="без багажа", offer_id="cheap"
    )
    cand = Offer(
        site="s", price=3200, duration="4 ч 50 м", transfers=1, baggage="без багажа", offer_id="cand"
    )
    fast = Offer(site="s", price=5000, duration="2 ч", transfers=0, offer_id="fast")

    cards = pick_cards([cheap, fast, cand], SearchCriteria())
    assert not any(t == "Золотая середина" for t, _ in cards)


def test_pick_cards_middle_dedup_with_best_balance():
    """(г) при hour_cost и совпадении с «Лучший баланс» не дублируется."""
    cheap = Offer(site="s", price=3000, duration="5 ч", transfers=1, offer_id="cheap")
    cand = Offer(site="s", price=3300, duration="3 ч", transfers=0, offer_id="cand")
    fast = Offer(site="s", price=6000, duration="2 ч", transfers=0, offer_id="fast")

    crit = SearchCriteria(hour_cost=300)
    cards = pick_cards([cheap, fast, cand], crit)

    # cand имеет лучший balance_score (3300 + 3*300 = 4200 против 5000 у cheap и 6600 у fast),
    # поэтому cand выбирается как «Лучший баланс», а карточка «Золотая середина» не дублируется.
    titles = [t for t, _ in cards]
    assert "Золотая середина" not in titles
    assert "Лучший баланс" in titles
    balance_offer = next(o for t, o in cards if t == "Лучший баланс")
    assert balance_offer is cand


def test_pick_cards_single_ticket():
    """(д) единственный билет → одна карточка."""
    o = Offer(site="s", price=3000, duration="3 ч", transfers=0, offer_id="single")
    cards = pick_cards([o], SearchCriteria())
    assert len(cards) == 1
    assert cards[0] == ("Самый дешёвый", o)


def test_title_notes_time_transfers_baggage():
    """(е) пометка про разницу в заголовке для времени и багажа (пересадки убраны)."""
    q = Query("MOW", "LED", date(2026, 12, 10))

    # 1. Выигрыш по времени: дешёвый 5 ч 00 м (300 м), середина 2 ч 50 м (170 м), выигрыш 130 м (2 ч 10 м >= 120 м и >= 25%), цена +240 ₽
    cheap_time = Offer(
        site="s", price=3000, duration="5 ч 00 м", transfers=0, baggage="без багажа", offer_id="c1"
    )
    middle_time = Offer(
        site="s", price=3240, duration="2 ч 50 м", transfers=0, baggage="без багажа", offer_id="m1"
    )
    note_time = format_middle_diff(cheap_time, middle_time)
    assert note_time == "(на 2 ч 10 м быстрее, +240 ₽)"
    card_time = offer_card(middle_time, q, number=3, title="Золотая середина", cheapest=cheap_time)
    assert "<b>3  ⚖️ Золотая середина (на 2 ч 10 м быстрее, +240 ₽)</b>" in card_time[0]

    # 2. Выигрыш только по пересадкам больше не делает билет серединой (по правилу владельца условие убрано)
    cheap_tr = Offer(
        site="s", price=3000, duration="5 ч", transfers=1, baggage="без багажа", offer_id="c2"
    )
    middle_tr = Offer(
        site="s", price=3120, duration="5 ч", transfers=0, baggage="без багажа", offer_id="m2"
    )
    note_tr = format_middle_diff(cheap_tr, middle_tr)
    assert note_tr == ""

    # 3. Выигрыш по багажу
    cheap_bag = Offer(
        site="s", price=3000, duration="5 ч", transfers=0, baggage="без багажа", offer_id="c3"
    )
    middle_bag = Offer(
        site="s", price=3300, duration="5 ч", transfers=0, baggage="с багажом", offer_id="m3"
    )
    note_bag = format_middle_diff(cheap_bag, middle_bag)
    assert note_bag == "(с багажом, +300 ₽)"
    card_bag = offer_card(middle_bag, q, number=3, title="Золотая середина", cheapest=cheap_bag)
    assert "<b>3  ⚖️ Золотая середина (с багажом, +300 ₽)</b>" in card_bag[0]


def test_build_compact_report_with_middle_card():
    """Интеграционный тест build_compact_report с формированием пометки из карточек."""
    q = Query("MOW", "LED", date(2026, 12, 10))
    # Обновлено под новые пороги: выигрыш во времени 2 ч 30 м (>= 120 мин и >= 25% от 5 ч)
    cheap = Offer(site="s", price=3000, duration="5 ч", transfers=1, baggage="без багажа", offer_id="c")
    middle = Offer(site="s", price=3120, duration="2 ч 30 м", transfers=0, baggage="без багажа", offer_id="m")
    fast = Offer(site="s", price=5000, duration="2 ч", transfers=0, offer_id="f")
    results = [SiteResult("s", True, [cheap, fast, middle])]

    chunks, flat, pool = build_compact_report(q, results, SearchCriteria())
    assert len(flat) == 3
    assert flat == [cheap, fast, middle]
    text = chunks[0]
    assert "<b>1  💰 Самый дешёвый</b>" in text
    assert "<b>2  ⚡ Самый быстрый</b>" in text
    assert "<b>3  ⚖️ Золотая середина (на 2 ч 30 м быстрее, +120 ₽)</b>" in text


def test_roundtrip_transfers_and_duration():
    """Учёт обратного плеча для суммарного времени (выигрыш >= 120 мин и >= 25%)."""
    cheap_rt = Offer(
        site="s",
        price=10000,
        duration="4 ч",
        transfers=1,
        back=Leg(duration="4 ч", transfers=1),
        offer_id="c_rt",
    )  # всего 8 ч
    middle_rt = Offer(
        site="s",
        price=11000,
        duration="2 ч 30 м",
        transfers=0,
        back=Leg(duration="2 ч 30 м", transfers=1),
        offer_id="m_rt",
    )  # всего 5 ч (выигрыш 3 ч = 180 мин >= 120 мин и >= 25% от 8 ч = 120 мин)
    fast_rt = Offer(
        site="s",
        price=20000,
        duration="2 ч",
        transfers=0,
        back=Leg(duration="2 ч", transfers=0),
        offer_id="f_rt",
    )

    cards = pick_cards([cheap_rt, fast_rt, middle_rt], SearchCriteria())
    titles = [t for t, _ in cards]
    assert titles == ["Самый дешёвый", "Самый быстрый", "Золотая середина"]
    note = format_middle_diff(cheap_rt, middle_rt)
    assert note == "(на 3 ч быстрее, +1 000 ₽)"
