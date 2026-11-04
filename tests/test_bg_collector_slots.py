"""Тесты новой логики кандидатов фонового сбора (слоты, горизонты, seed-маршруты, приоритеты)."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from avia.admin_panel import build_overview, build_tickets
from avia.bg_collector import BackgroundCollector
from avia.config import Settings
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult


def _make_settings(**kwargs) -> Settings:
    defaults = {
        "sites": [],
        "tg_token": "secret",
        "tg_allowed_ids": {1},
        "bg_collect": True,
        "bg_collect_interval_minutes": 20,
        "bg_collect_max_per_day": 120,
        "bg_collect_recheck_hours": 24,
        "bg_collect_top_routes": 12,
        "bg_collect_seed_routes": ["MOW-LED", "MOW-AER"],
        "bg_collect_horizons_days": [7, 14, 28, 56, 90],
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def test_candidates_include_seed_routes_when_history_empty(tmp_path):
    """При пустой истории кандидаты формируются из seed-маршрутов в обе стороны."""
    history = PriceHistory(tmp_path / "h.sqlite")
    fixed_today = date(2026, 11, 1)
    fixed_now = 1000000.0

    candidates = history.collect_slot_candidates(
        limit=20,
        today=fixed_today,
        now=fixed_now,
        seed_routes=["MOW-LED"],
        horizons_days=[7, 14],
        top_routes=5,
    )

    assert len(candidates) > 0
    # Оба направления MOW->LED и LED->MOW
    origins = {c.origin for c in candidates}
    destinations = {c.destination for c in candidates}
    assert origins == {"MOW", "LED"}
    assert destinations == {"MOW", "LED"}

    # Только в одну сторону и 1 взрослый
    for c in candidates:
        assert c.return_date is None
        assert c.adults == 1
        assert c.currency == "RUB"
        assert c.depart in (fixed_today + timedelta(days=7), fixed_today + timedelta(days=14))


def test_empty_seed_yields_no_candidates_when_history_empty(tmp_path):
    """При пустой истории и пустом списке seed кандидатов нет."""
    history = PriceHistory(tmp_path / "h.sqlite")
    fixed_today = date(2026, 11, 1)
    fixed_now = 1000000.0

    candidates = history.collect_slot_candidates(
        limit=10,
        today=fixed_today,
        now=fixed_now,
        seed_routes=[],
        horizons_days=[7, 14],
        top_routes=5,
    )
    assert candidates == []


def test_horizons_give_fixed_dates_not_floating_day_by_day(tmp_path):
    """Горизонты дают фиксированные даты: дата вылета остаётся той же день за днём."""
    history = PriceHistory(tmp_path / "h.sqlite")
    search_fn = AsyncMock(return_value=[])
    is_idle = MagicMock(return_value=True)

    cur_today = date(2026, 11, 1)
    cur_clock = 100000.0

    settings = _make_settings(
        bg_collect_seed_routes=["MOW-LED"],
        bg_collect_horizons_days=[7],
        bg_collect_top_routes=0,
    )

    collector = BackgroundCollector(
        history=history,
        search_fn=search_fn,
        is_idle=is_idle,
        settings=settings,
        clock=lambda: cur_clock,
        today=lambda: cur_today,
    )

    # 1. День 0 (2026-11-01): горизонт 7 дней -> дата вылета 2026-11-08
    c0 = collector.get_candidates(limit=5)
    assert len(c0) > 0
    target_date = date(2026, 11, 8)
    for q in c0:
        assert q.depart == target_date

    # 2. День 1 (2026-11-02): дата вылета должна ОСТАТЬСЯ 2026-11-08 (не стать 2026-11-09)
    cur_today = date(2026, 11, 2)
    cur_clock += 86400.0

    c1 = collector.get_candidates(limit=5)
    for q in c1:
        assert q.depart == target_date

    # 3. День 5 (2026-11-06): дата вылета всё ещё 2026-11-08
    cur_today = date(2026, 11, 6)
    cur_clock += 4 * 86400.0

    c5 = collector.get_candidates(limit=5)
    for q in c5:
        assert q.depart == target_date


def test_departed_pair_is_replaced_by_new_target(tmp_path):
    """Улетевшая пара (depart < today) заменяется новым слотом (сегодня + горизонт)."""
    history = PriceHistory(tmp_path / "h.sqlite")
    search_fn = AsyncMock(return_value=[])
    is_idle = MagicMock(return_value=True)

    cur_today = date(2026, 11, 1)
    cur_clock = 100000.0

    settings = _make_settings(
        bg_collect_seed_routes=["MOW-LED"],
        bg_collect_horizons_days=[7],
        bg_collect_top_routes=0,
    )

    collector = BackgroundCollector(
        history=history,
        search_fn=search_fn,
        is_idle=is_idle,
        settings=settings,
        clock=lambda: cur_clock,
        today=lambda: cur_today,
    )

    c_init = collector.get_candidates(limit=5)
    assert all(q.depart == date(2026, 11, 8) for q in c_init)

    # Рейс улетел: наступило 2026-11-09 (depart 2026-11-08 < 2026-11-09)
    cur_today = date(2026, 11, 9)
    cur_clock += 8 * 86400.0

    c_new = collector.get_candidates(limit=5)
    expected_new_date = date(2026, 11, 9) + timedelta(days=7)  # 2026-11-16
    assert all(q.depart == expected_new_date for q in c_new)


def test_priority_by_check_age_and_days_to_departure(tmp_path):
    """Приоритет у пар, которые дольше всего не проверяли, затем у кого меньше дней до вылета."""
    history = PriceHistory(tmp_path / "h.sqlite")
    today = date(2026, 11, 1)
    now_ts = 1000000.0

    # Создаём историю:
    # Пара 1: MOW->LED на 2026-11-08 проверена 40 часов назад
    q1 = Query(origin="MOW", destination="LED", depart=date(2026, 11, 8))
    history.annotate(
        q1,
        [SiteResult("site", True, [Offer("site", 5000, offer_id="o1")])],
        now=now_ts - 40 * 3600,
    )

    # Пара 2: MOW->AER на 2026-11-08 проверена 25 часов назад
    q2 = Query(origin="MOW", destination="AER", depart=date(2026, 11, 8))
    history.annotate(
        q2,
        [SiteResult("site", True, [Offer("site", 7000, offer_id="o2")])],
        now=now_ts - 25 * 3600,
    )

    # Пара 3: SVX->AER на 2026-11-08 проверена 5 часов назад (recheck_hours=24 -> не должна попасть!)
    q3 = Query(origin="SVX", destination="AER", depart=date(2026, 11, 8))
    history.annotate(
        q3,
        [SiteResult("site", True, [Offer("site", 8000, offer_id="o3")])],
        now=now_ts - 5 * 3600,
    )

    # Пара 4: никогда не проверялась (observed_at = 0.0)
    slots = {
        ("MOW", "LED", 7): date(2026, 11, 8),
        ("MOW", "AER", 7): date(2026, 11, 8),
        ("SVX", "AER", 7): date(2026, 11, 8),
        ("KUF", "EVN", 7): date(2026, 11, 8),  # никогда не проверялась
        ("KUF", "EVN", 14): date(2026, 11, 15),  # никогда не проверялась, но дальше до вылета
    }

    candidates = history.collect_slot_candidates(
        limit=25,
        today=today,
        now=now_ts,
        recheck_seconds=24 * 3600,
        seed_routes=["MOW-LED", "MOW-AER", "SVX-AER", "KUF-EVN"],
        horizons_days=[7, 14],
        top_routes=0,
        slots=slots,
    )

    # 1. SVX->AER на 2026-11-08 проверена 5 часов назад -> отфильтрована (recheck_seconds=24ч)
    svx_aer = [c for c in candidates if c.origin == "SVX" and c.destination == "AER"]
    assert len(svx_aer) == 0 or all(c.depart != date(2026, 11, 8) for c in svx_aer)

    # 2. Непроверенные пары идут ПЕРВЫМИ
    # Среди непроверенных: вылет через 7 дней раньше, чем через 14 дней
    unseen_7 = [c for c in candidates if c.origin == "KUF" and c.destination == "EVN" and c.depart == date(2026, 11, 8)]
    unseen_14 = [c for c in candidates if c.origin == "KUF" and c.destination == "EVN" and c.depart == date(2026, 11, 15)]
    assert candidates.index(unseen_7[0]) < candidates.index(unseen_14[0])

    # 3. Среди проверенных: q1 (40ч назад) раньше, чем q2 (25ч назад)
    seen_q1 = [c for c in candidates if c.origin == "MOW" and c.destination == "LED" and c.depart == date(2026, 11, 8)]
    seen_q2 = [c for c in candidates if c.origin == "MOW" and c.destination == "AER" and c.depart == date(2026, 11, 8)]
    assert candidates.index(seen_q1[0]) < candidates.index(seen_q2[0])


@pytest.mark.asyncio
async def test_run_once_daily_limit(tmp_path):
    """Сбор останавливается при достижении суточного лимита."""
    history = PriceHistory(tmp_path / "h.sqlite")
    search_fn = AsyncMock(return_value=[])
    is_idle = MagicMock(return_value=True)

    today = date(2026, 11, 1)
    clock_val = 100000.0

    settings = _make_settings(
        bg_collect_max_per_day=3,
        bg_collect_seed_routes=["MOW-LED"],
        bg_collect_horizons_days=[7, 14, 28],
    )

    collector = BackgroundCollector(
        history=history,
        search_fn=search_fn,
        is_idle=is_idle,
        settings=settings,
        clock=lambda: clock_val,
        today=lambda: today,
    )

    assert await collector.run_once() is True
    assert collector.done_today == 1

    assert await collector.run_once() is True
    assert collector.done_today == 2

    assert await collector.run_once() is True
    assert collector.done_today == 3

    # Лимит достигнут -> run_once возвращает False и не ищет
    assert await collector.run_once() is False
    assert collector.done_today == 3
    assert search_fn.call_count == 3


def test_slots_persistence_across_collector_instances(tmp_path):
    """Слоты сохраняются в таблице bg_collect_slots БД и восстанавливаются при перезапуске."""
    db_path = tmp_path / "prices.sqlite"
    history1 = PriceHistory(db_path)
    today = date(2026, 11, 1)

    settings = _make_settings(
        bg_collect_seed_routes=["MOW-LED"],
        bg_collect_horizons_days=[7, 28],
    )

    collector1 = BackgroundCollector(
        history=history1,
        search_fn=AsyncMock(),
        is_idle=lambda: True,
        settings=settings,
        today=lambda: today,
    )

    # Инициализируем слоты
    pairs1 = collector1._sync_slots()
    assert len(pairs1) > 0

    # Создаём новый collector на той же БД (симуляция перезапуска)
    history2 = PriceHistory(db_path)
    today_next = date(2026, 11, 2)
    collector2 = BackgroundCollector(
        history=history2,
        search_fn=AsyncMock(),
        is_idle=lambda: True,
        settings=settings,
        today=lambda: today_next,
    )

    # Проверяем, что даты слотов восстановились из БД и не сдвинулись
    slots2 = collector2._slots
    assert len(slots2) > 0
    for key, dep in slots2.items():
        assert dep == today + timedelta(days=key[2])


def test_admin_panel_bg_collect_display():
    """/admin отображает число активных пар, число маршрутов и сделано сегодня из лимита с html.escape."""
    # 1. build_overview
    ctx_over = {
        "uptime_seconds": 3600,
        "git_version": "<commit>",
        "users_total": 10,
        "users_active_24h": 2,
        "users_active_7d": 5,
        "users_new_24h": 1,
        "searches_today": 8,
        "bg_searches": "12 из 120",
        "bg_active_pairs": 60,
        "bg_routes": 12,
        "active_subs": 3,
        "busy_searches": 0,
        "last_backup_at": None,
        "now": 1000.0,
        "state_size_mb": 2.5,
    }
    txt_over = build_overview(ctx_over)
    assert "Поисков сегодня: людей — 8, фоновых — 12 из 120" in txt_over
    assert "Фоновый сбор: 60 активных пар (маршрут+дата) · 12 маршрутов · 12 из 120 сделано сегодня" in txt_over
    assert "&lt;commit&gt;" in txt_over

    # 2. build_tickets
    ctx_tickets = {
        "routes_count": 12,
        "searches_count": 100,
        "avg_days_to_departure": 21.0,
        "popular_routes": [],
        "baggage": {},
        "site_share": [],
        "forecast": None,
        "bg_active_pairs": 60,
        "bg_routes": 12,
        "bg_done": "12 из 120",
    }
    txt_tickets = build_tickets(ctx_tickets)
    assert "<b>Фоновый сбор:</b>" in txt_tickets
    assert "Активных пар (маршрут+дата): 60" in txt_tickets
    assert "Различных маршрутов: 12" in txt_tickets
    assert "Сделано сегодня: 12 из 120" in txt_tickets
