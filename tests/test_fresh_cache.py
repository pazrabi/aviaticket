"""Тесты для FreshCache (шаг 13 PLAN_SPEED.md):
- put/get с max_age и TTL 15 минут
- ключ различает даты, пассажиров, валюту, не зависит от чата и пост-фильтров
- успешный фон кладёт, быстрый поиск берёт из кеша и не вызывает сайт
- deep и фон игнорируют кеш на чтение
- результат старше 10 минут не используется
- пометки в карточке и шапке/подвале (🕒)
- FRESH_MAX_AGE_SECONDS=0 отключает использование кеша
- не сохраняются ошибки и пустые результаты
- метрики: site_runs не пишутся, searches.cls='fast'
- история цен: annotate не дублирует записи для результатов с cached_age
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from avia.config import Settings, SiteConfig
from avia.fresh_cache import FreshCache
from avia.formatting import build_compact_report, format_cached_age_note, offer_card
from avia.history import PriceHistory
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.pipeline import search


def _dummy_query(**kwargs) -> Query:
    base = {
        "origin": "MOW",
        "destination": "LED",
        "depart": date(2026, 12, 1),
        "adults": 1,
        "currency": "RUB",
        "return_date": None,
    }
    base.update(kwargs)
    return Query(**base)


def _dummy_offer(site: str = "aviasales", price: int = 5000, offer_id: str = "off1") -> Offer:
    return Offer(
        site=site,
        price=price,
        currency="RUB",
        airline="SU",
        depart_time="10:00",
        arrive_time="11:30",
        duration="1ч 30м",
        transfers=0,
        baggage="с багажом",
        url="https://example.com/ticket",
        offer_id=offer_id,
    )


def test_fresh_cache_put_get_with_max_age():
    cache = FreshCache(ttl_seconds=900, max_entries=300)
    q = _dummy_query()
    res = SiteResult(
        site="aviasales",
        ok=True,
        offers=[_dummy_offer()],
        elapsed=2.5,
    )

    now = 1000.0
    cache.put(q, "aviasales", res, ts=now)

    # get при возрасте 200 с (< max_age 600)
    hit = cache.get(q, "aviasales", max_age=600.0, now=now + 200.0)
    assert hit is not None
    cached_res, age = hit
    assert age == pytest.approx(200.0)
    assert cached_res.cached_age == pytest.approx(200.0)
    assert cached_res.site == "aviasales"
    assert len(cached_res.offers) == 1
    assert getattr(cached_res.offers[0], "cached_age", None) == pytest.approx(200.0)

    # Запись не мутирует объект в кеше
    cached_res.offers.clear()
    hit2 = cache.get(q, "aviasales", max_age=600.0, now=now + 200.0)
    assert hit2 is not None
    assert len(hit2[0].offers) == 1

    # get при возрасте 200 с > max_age 150 -> None
    assert cache.get(q, "aviasales", max_age=150.0, now=now + 200.0) is None

    # max_age <= 0 -> None
    assert cache.get(q, "aviasales", max_age=0.0, now=now + 50.0) is None
    assert cache.get(q, "aviasales", max_age=-10.0, now=now + 50.0) is None


def test_fresh_cache_key_differentiation():
    cache = FreshCache()
    q_base = _dummy_query()
    res = SiteResult(site="aviasales", ok=True, offers=[_dummy_offer()])
    now = 1000.0
    cache.put(q_base, "aviasales", res, ts=now)

    # Тот же запрос -> найдено
    assert cache.get(q_base, "aviasales", max_age=600, now=now) is not None

    # Другой сайт
    assert cache.get(q_base, "yandex", max_age=600, now=now) is None

    # Другая дата вылета
    q_diff_dep = _dummy_query(depart=date(2026, 12, 2))
    assert cache.get(q_diff_dep, "aviasales", max_age=600, now=now) is None

    # Дата возврата
    q_with_ret = _dummy_query(return_date=date(2026, 12, 10))
    assert cache.get(q_with_ret, "aviasales", max_age=600, now=now) is None

    # Другое число пассажиров
    q_diff_pax = _dummy_query(adults=2)
    assert cache.get(q_diff_pax, "aviasales", max_age=600, now=now) is None

    # Другая валюта
    q_diff_curr = _dummy_query(currency="USD")
    assert cache.get(q_diff_curr, "aviasales", max_age=600, now=now) is None

    # Другой маршрут
    q_diff_route = _dummy_query(destination="AER")
    assert cache.get(q_diff_route, "aviasales", max_age=600, now=now) is None


def test_fresh_cache_does_not_store_errors_or_empty():
    cache = FreshCache()
    q = _dummy_query()

    # ok=False
    r_not_ok = SiteResult(site="as", ok=False, offers=[_dummy_offer()])
    assert not cache.put(q, "as", r_not_ok)
    assert len(cache) == 0

    # offers=[]
    r_empty = SiteResult(site="as", ok=True, offers=[])
    assert not cache.put(q, "as", r_empty)
    assert len(cache) == 0

    # error установлен
    r_err = SiteResult(site="as", ok=True, offers=[_dummy_offer()], error="не успел к дедлайну")
    assert not cache.put(q, "as", r_err)
    assert len(cache) == 0


def test_fresh_cache_age_over_10_minutes_not_used():
    cache = FreshCache(ttl_seconds=900)
    q = _dummy_query()
    res = SiteResult(site="aviasales", ok=True, offers=[_dummy_offer()])
    now = 1000.0
    cache.put(q, "aviasales", res, ts=now)

    # 599 секунд (меньше 10 минут) -> используется
    assert cache.get(q, "aviasales", max_age=600.0, now=now + 599.0) is not None

    # 601 секунда (старше 10 минут) -> не используется
    assert cache.get(q, "aviasales", max_age=600.0, now=now + 601.0) is None

    # 901 секунда (> TTL 15 минут) -> удаляется из кеша
    assert cache.get(q, "aviasales", max_age=1200.0, now=now + 901.0) is None
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_pipeline_bg_search_puts_and_fast_search_takes_from_cache(monkeypatch):
    cache = FreshCache()
    q = _dummy_query()
    site = SiteConfig(
        name="aviasales",
        url_template="https://example.com/{origin}/{destination}",
        enabled=True,
        phone_wait_seconds=5,
    )
    settings = Settings(sites=[site], search_parallel_live=2, search_parallel_bg=2)

    call_count = 0

    async def mock_run_site(s, query, setts, **kwargs):
        nonlocal call_count
        call_count += 1
        return SiteResult(
            site=s.name,
            ok=True,
            offers=[_dummy_offer(site=s.name)],
            elapsed=3.0,
        )

    monkeypatch.setattr("avia.pipeline._run_site", mock_run_site)

    # 1. Фоновый запуск (priority=2, fresh_max_age=0): всегда ходит на сайт и кладёт в кеш
    bg_results = await search(
        q,
        settings,
        priority=2,
        fresh_cache=cache,
        fresh_max_age=0.0,
    )
    assert len(bg_results) == 1
    assert bg_results[0].cached_age is None
    assert call_count == 1
    assert len(cache) == 1

    # 2. Быстрый поиск пользователя (priority=0, deep=False, fresh_max_age=600):
    # берёт из кеша, НЕ вызывает _run_site
    on_site_done_called = False

    def on_done(r):
        nonlocal on_site_done_called
        on_site_done_called = True

    fast_results = await search(
        q,
        settings,
        priority=0,
        deep=False,
        on_site_done=on_done,
        fresh_cache=cache,
        fresh_max_age=600.0,
    )
    assert len(fast_results) == 1
    assert call_count == 1  # Сайт НЕ вызывался повторно!
    assert fast_results[0].cached_age is not None
    assert on_site_done_called is True


@pytest.mark.asyncio
async def test_pipeline_deep_and_bg_ignore_cache_for_reading(monkeypatch):
    cache = FreshCache()
    q = _dummy_query()
    site = SiteConfig(
        name="aviasales",
        url_template="https://example.com/{origin}/{destination}",
        enabled=True,
        phone_wait_seconds=5,
    )
    settings = Settings(sites=[site])

    # Заполняем кеш
    cache.put(q, "aviasales", SiteResult(site="aviasales", ok=True, offers=[_dummy_offer()]))

    call_count = 0

    async def mock_run_site(s, query, setts, **kwargs):
        nonlocal call_count
        call_count += 1
        return SiteResult(site=s.name, ok=True, offers=[_dummy_offer()], elapsed=1.0)

    monkeypatch.setattr("avia.pipeline._run_site", mock_run_site)

    # Глубокий поиск (deep=True) идёт на сайт даже при наличии кеша
    await search(q, settings, priority=0, deep=True, fresh_cache=cache, fresh_max_age=0.0)
    assert call_count == 1

    # Фоновый поиск (priority=2) идёт на сайт
    await search(q, settings, priority=2, fresh_cache=cache, fresh_max_age=0.0)
    assert call_count == 2

    # Поиск подписок (priority=1) идёт на сайт
    await search(q, settings, priority=1, fresh_cache=cache, fresh_max_age=0.0)
    assert call_count == 3


@pytest.mark.asyncio
async def test_fresh_max_age_zero_disables_cache(monkeypatch):
    cache = FreshCache()
    q = _dummy_query()
    site = SiteConfig(
        name="aviasales",
        url_template="https://example.com/{origin}/{destination}",
        enabled=True,
    )
    settings = Settings(sites=[site])

    cache.put(q, "aviasales", SiteResult(site="aviasales", ok=True, offers=[_dummy_offer()]))

    call_count = 0

    async def mock_run_site(s, query, setts, **kwargs):
        nonlocal call_count
        call_count += 1
        return SiteResult(site=s.name, ok=True, offers=[_dummy_offer()], elapsed=1.0)

    monkeypatch.setattr("avia.pipeline._run_site", mock_run_site)

    # fresh_max_age=0 отключает взятие из кеша
    results = await search(q, settings, priority=0, fresh_cache=cache, fresh_max_age=0.0)
    assert call_count == 1
    assert results[0].cached_age is None


def test_formatting_card_notes_and_header_footer():
    from avia.criteria import SearchCriteria

    q = _dummy_query()

    # 1. Пометка меньше минуты -> «🕒 данные только что»
    assert format_cached_age_note(10.0) == "🕒 данные только что"
    assert format_cached_age_note(59.9) == "🕒 данные только что"

    # 2. Пометка >= 1 минуты -> округление вверх
    assert format_cached_age_note(60.0) == "🕒 данные 1 мин назад"
    assert format_cached_age_note(61.0) == "🕒 данные 2 мин назад"
    assert format_cached_age_note(120.0) == "🕒 данные 2 мин назад"
    assert format_cached_age_note(590.0) == "🕒 данные 10 мин назад"

    # 3. В карточке offer_card
    o_fresh = _dummy_offer()
    card = offer_card(o_fresh, q, number=1, cached_age=30.0)
    card_text = "\n".join(card)
    assert "🕒 данные только что" in card_text

    card_min = offer_card(o_fresh, q, number=1, cached_age=150.0)
    card_min_text = "\n".join(card_min)
    assert "🕒 данные 3 мин назад" in card_min_text

    # 4. В build_compact_report: все сайты из кеша
    o1 = _dummy_offer(site="aviasales", price=5000, offer_id="as1")
    o1.duration = "2ч 00м"
    o2 = _dummy_offer(site="yandex", price=6000, offer_id="ya1")
    o2.airline = "DP"
    o2.depart_time = "14:00"
    o2.arrive_time = "15:00"
    o2.duration = "1ч 00м"  # Самый быстрый!
    res_cached1 = SiteResult(site="aviasales", ok=True, offers=[o1], cached_age=45.0)
    res_cached2 = SiteResult(site="yandex", ok=True, offers=[o2], cached_age=180.0)

    chunks, flat, pool = build_compact_report(q, [res_cached1, res_cached2], SearchCriteria())
    full_text = "\n".join(chunks)
    assert "🕒 данные только что" in full_text
    assert "🕒 данные 3 мин назад" in full_text
    assert "🕒 Показаны свежие данные (не старше 10 минут). Для нового запроса нажмите «🔎 Глубокий поиск»" in full_text

    # 5. Смешанный ответ (один живой, один из кеша) -> общей строки нет
    res_live = SiteResult(site="yandex", ok=True, offers=[o2], cached_age=None)
    chunks_mixed, _, _ = build_compact_report(q, [res_cached1, res_live], SearchCriteria())
    mixed_text = "\n".join(chunks_mixed)
    assert "🕒 данные только что" in mixed_text
    assert "🕒 Показаны свежие данные" not in mixed_text


def test_metrics_and_history_no_duplicates_for_cached(tmp_path):
    q = _dummy_query()

    # 1. Проверяем метрики: при cached_age запись site_runs НЕ создаётся
    metrics_path = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(metrics_path)

    res_cached = SiteResult(
        site="aviasales",
        ok=True,
        offers=[_dummy_offer()],
        cached_age=100.0,
    )
    metrics.record_results("user", [res_cached])

    with metrics._connect() as db:
        cnt = db.execute("SELECT COUNT(*) FROM site_runs").fetchone()[0]
        assert cnt == 0, "site_runs не должен создаваться для результатов из кеша"

    # searches пишется с cls='fast'
    metrics.record_search("user", 0.5, [res_cached])
    with metrics._connect() as db:
        row = db.execute("SELECT cls, sites_ok FROM searches").fetchone()
        assert row[0] == "fast"
        assert row[1] == 1

    # 2. Проверяем историю: history.annotate не удваивает записи
    hist_path = tmp_path / "history.sqlite"
    history = PriceHistory(hist_path)

    # Первичный запуск: пишет запись в price_history
    res_live = SiteResult(site="aviasales", ok=True, offers=[_dummy_offer(offer_id="id_1", price=5000)])
    history.annotate(q, [res_live], now=1000.0)

    with history._connect() as db:
        cnt_hist = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        assert cnt_hist == 1

    # Повторный запуск из кеша: annotate проставляет prev_price, но не вставляет в price_history
    res_from_cache = SiteResult(
        site="aviasales",
        ok=True,
        offers=[_dummy_offer(offer_id="id_1", price=5000)],
        cached_age=60.0,
    )
    history.annotate(q, [res_from_cache], now=1060.0)

    # Проверяем, что офер аннотирован
    assert res_from_cache.offers[0].prev_price == 5000
    # Проверяем, что в БД всё ещё ровно 1 запись
    with history._connect() as db:
        cnt_hist2 = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        assert cnt_hist2 == 1, "Запись из кеша не должна добавляться в price_history"
