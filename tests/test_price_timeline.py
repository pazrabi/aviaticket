"""Тесты хронологии цен price_timeline (построение, хранение, отключение через конфиг)."""
from __future__ import annotations

import asyncio
from datetime import date
import json
from unittest.mock import MagicMock
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.metrics import MetricsStore
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import _from_browser, _timeline_tasks
from avia.price_timeline import build_timeline


@pytest.mark.asyncio
async def test_build_timeline_prices():
    """(б) build_timeline на поддельном парсере: цены 12000 (t=2), 9000 (t=5), 9500 (t=8)

    дают точки с min 12000, 9000, 9000 и final_min 9000, t_last 8.
    """
    def fake_parser(snapshot: Snapshot, q: Query) -> list[Offer]:
        offers: list[Offer] = []
        for url, body in snapshot.json_payloads:
            try:
                data = json.loads(body)
                if "price" in data:
                    offers.append(Offer(site="test_site", price=int(data["price"])))
            except Exception:
                pass
        return offers

    PARSERS["test_fake_parser_b"] = fake_parser
    site = SiteConfig(name="test_site", url_template="https://test.site/{origin}", parser="test_fake_parser_b")
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    settings = Settings(sites=[site])

    snap = Snapshot(
        url="https://test.site/search",
        text="страница с выдачей",
        json_payloads=[
            ("https://test.site/api/1", json.dumps({"price": 12000})),
            ("https://test.site/api/2", json.dumps({"price": 9000})),
            ("https://test.site/api/3", json.dumps({"price": 9500})),
        ],
        payload_times=[2, 5, 8],
        woke=True,
        t_first_match=1.5,
    )

    tl = await build_timeline(site, snap, q, settings)
    assert tl is not None
    assert tl["final_min"] == 9000
    assert tl["t_last"] == 8
    assert tl["woke"] is True
    assert tl["t_first_match"] == 1.5
    assert len(tl["points"]) == 3
    assert tl["points"][0] == {"t": 2, "min": 12000, "n": 1}
    assert tl["points"][1] == {"t": 5, "min": 9000, "n": 2}
    assert tl["points"][2] == {"t": 8, "min": 9000, "n": 3}


@pytest.mark.asyncio
async def test_build_timeline_all_none_times():
    """(в) все t None → None."""
    site = SiteConfig(name="test_site", url_template="https://test.site/{origin}", parser="test_fake_parser_b")
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    settings = Settings(sites=[site])

    # Случай 1: все t None
    snap = Snapshot(
        url="https://test.site/search",
        text="текст",
        json_payloads=[("url1", "body1"), ("url2", "body2")],
        payload_times=[None, None],
    )
    assert await build_timeline(site, snap, q, settings) is None

    # Случай 2: payload_times пустой
    snap_empty_times = Snapshot(
        url="https://test.site/search",
        text="текст",
        json_payloads=[("url1", "body1")],
        payload_times=[],
    )
    assert await build_timeline(site, snap_empty_times, q, settings) is None

    # Случай 3: json_payloads пустой
    snap_no_payloads = Snapshot(
        url="https://test.site/search",
        text="текст",
        json_payloads=[],
        payload_times=[],
    )
    assert await build_timeline(site, snap_no_payloads, q, settings) is None


@pytest.mark.asyncio
async def test_build_timeline_more_than_40_points():
    """(г) более 40 ответов → не больше 40 точек и последняя включена."""
    def fake_counter_parser(snapshot: Snapshot, q: Query) -> list[Offer]:
        offers: list[Offer] = []
        for url, body in snapshot.json_payloads:
            offers.append(Offer(site="test_site", price=int(body)))
        return offers

    PARSERS["test_counter_parser_g"] = fake_counter_parser
    site = SiteConfig(name="test_site", url_template="https://test.site/{origin}", parser="test_counter_parser_g")
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    settings = Settings(sites=[site])

    n_answers = 50
    json_payloads = [(f"https://test.site/api/{i}", str(10000 - i * 10)) for i in range(1, n_answers + 1)]
    payload_times = [float(i) for i in range(1, n_answers + 1)]

    snap = Snapshot(
        url="https://test.site/search",
        text="текст",
        json_payloads=json_payloads,
        payload_times=payload_times,
    )

    tl = await build_timeline(site, snap, q, settings)
    assert tl is not None
    assert len(tl["points"]) == 40
    assert len(tl["points"]) <= 40
    # Проверяем, что последняя точка обязательно включена
    assert tl["points"][-1]["t"] == 50.0
    assert tl["t_last"] == 50.0
    assert tl["final_min"] == 10000 - 50 * 10


def test_metrics_record_and_timelines_and_prune(tmp_path):
    """(д) metrics.record_timeline/timelines возвращают то, что записали, prune удаляет старое."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    now = 20_000_000.0
    day = 86400.0

    points1 = [{"t": 2.0, "min": 12000, "n": 1}, {"t": 5.0, "min": 9000, "n": 2}]
    points2 = [{"t": 3.0, "min": 15000, "n": 1}]

    # Запись старой хронологии (100 дней назад)
    store.record_timeline(
        search_id="search_old",
        site="site_a",
        device="realme",
        woke=True,
        t_first_match=1.2,
        t_last=5.0,
        final_min=9000,
        points=points1,
        ts=now - 100 * day,
    )

    # Запись свежей хронологии (10 дней назад)
    store.record_timeline(
        search_id="search_fresh",
        site="site_a",
        device="pixel",
        woke=False,
        t_first_match=2.0,
        t_last=3.0,
        final_min=15000,
        points=points2,
        ts=now - 10 * day,
    )

    # Проверяем получение timelines
    all_tl = store.timelines()
    assert len(all_tl) == 2

    # Проверяем фильтрацию по сайту
    assert len(store.timelines(site="site_a")) == 2
    assert len(store.timelines(site="site_other")) == 0

    # Проверяем корректность возвращенных данных
    fresh = [x for x in all_tl if x["search_id"] == "search_fresh"][0]
    assert fresh["site"] == "site_a"
    assert fresh["device"] == "pixel"
    assert fresh["woke"] is False
    assert fresh["t_first_match"] == 2.0
    assert fresh["t_last"] == 3.0
    assert fresh["final_min"] == 15000.0
    assert fresh["points"] == points2

    old = [x for x in all_tl if x["search_id"] == "search_old"][0]
    assert old["device"] == "realme"
    assert old["woke"] is True
    assert old["t_first_match"] == 1.2
    assert old["t_last"] == 5.0
    assert old["final_min"] == 9000.0
    assert old["points"] == points1

    # Запускаем prune(keep_days=90)
    store.prune(keep_days=90, now=now)

    # Старая запись удалена, свежая осталась
    rem = store.timelines()
    assert len(rem) == 1
    assert rem[0]["search_id"] == "search_fresh"


@pytest.mark.asyncio
async def test_price_timeline_disabled_by_config(monkeypatch, tmp_path):
    """(е) PRICE_TIMELINE=0 — запись не вызывается."""
    # 1. Проверяем config: env PRICE_TIMELINE=0 даёт price_timeline == False
    monkeypatch.setenv("PRICE_TIMELINE", "0")
    settings = load_settings()
    assert settings.price_timeline is False

    # 2. Проверяем в pipeline: при price_timeline=False record_timeline не вызывается
    mock_metrics = MagicMock()
    settings.metrics = mock_metrics
    settings.storage_dir = tmp_path

    def fake_parser(snapshot: Snapshot, q: Query) -> list[Offer]:
        return [Offer(site="test_site", price=5000)]

    PARSERS["test_fake_parser_e"] = fake_parser
    site = SiteConfig(
        name="test_site",
        url_template="https://test.site/{origin}",
        parser="test_fake_parser_e",
        phone="dev1",
    )

    snap = Snapshot(
        url="https://test.site/search",
        text="Страница",
        json_payloads=[("url", '{"price": 5000}')],
        payload_times=[1.5],
    )

    async def fake_fetch(*args, **kwargs):
        if "stats" in kwargs and kwargs["stats"] is not None:
            kwargs["stats"]["device"] = "dev1"
        return snap, True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch)
    monkeypatch.setattr("avia.pipeline.phone_enabled", lambda s, st: True)
    monkeypatch.setattr("avia.pipeline.candidates", lambda s, st, **kw: ["dev1"])

    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    offers, page_url, partial = await _from_browser(site, q, settings, search_id="search_123")
    assert len(offers) == 1

    if _timeline_tasks:
        await asyncio.gather(*list(_timeline_tasks))
    await asyncio.sleep(0.05)

    assert mock_metrics.record_timeline.call_count == 0

    # 3. При включенном price_timeline=True запись вызывается
    settings.price_timeline = True
    offers2, page_url2, partial2 = await _from_browser(site, q, settings, search_id="search_456")
    assert len(offers2) == 1

    if _timeline_tasks:
        await asyncio.gather(*list(_timeline_tasks))
    await asyncio.sleep(0.05)

    assert mock_metrics.record_timeline.call_count == 1
    call_kwargs = mock_metrics.record_timeline.call_args[1]
    assert call_kwargs.get("search_id") == "search_456"
    assert call_kwargs.get("device") == "dev1"
    assert call_kwargs.get("site") == "test_site"
