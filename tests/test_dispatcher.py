"""Тесты модуля dispatcher (общая раскладка телефонов для живого поиска)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from typing import Any
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.dispatcher import plan_live_assignment
from avia.guard import SearchGuard
from avia.models import Query
from avia.phones import PhoneRegistry
from avia.pipeline import _from_browser, search


@dataclass
class FakeSpeedStats:
    cost: float | None = None
    n: int = 10
    p_lower: float = 0.8


@dataclass
class FakePairStatus:
    in_quarantine: bool = False
    awaiting_trial: bool = False
    consecutive_captchas: int = 0


class FakeSpeedTable:
    def __init__(
        self,
        stats_map: dict[tuple[str, str], FakeSpeedStats] | None = None,
        pair_map: dict[tuple[str, str], FakePairStatus] | None = None,
        site_medians: dict[str, float] | None = None,
        phone_multipliers: dict[tuple[str, str], float] | None = None,
    ):
        self._stats = stats_map or {}
        self._pairs = pair_map or {}
        self._site_medians = site_medians or {}
        self._phone_multipliers = phone_multipliers or {}
        self._phone_overall_multipliers = {}

    def get_stats(self, site: str, phone: str, class_name: str, now: float | None = None) -> FakeSpeedStats:
        key = (site, phone)
        if key in self._stats:
            return self._stats[key]
        return FakeSpeedStats(cost=None, n=0, p_lower=0.0)

    def get_pair_status(self, site: str, phone: str, now: float | None = None) -> FakePairStatus:
        key = (site, phone)
        if key in self._pairs:
            return self._pairs[key]
        return FakePairStatus()

    def get_site_median(self, site: str, class_name: str) -> float | None:
        return self._site_medians.get(site)


@pytest.fixture(autouse=True)
def clean_pipeline_guard():
    from avia.pipeline import guard as default_guard

    default_guard.clear()
    orig_interval = default_guard.min_interval
    orig_retry = default_guard.retry_delay
    default_guard.min_interval = 0.0
    default_guard.retry_delay = 0.0
    yield
    default_guard.clear()
    default_guard.min_interval = orig_interval
    default_guard.retry_delay = orig_retry


def test_two_sites_compete_for_best_phone_min_max():
    """(а) Два сайта хотят один лучший телефон, раскладка отдаёт его тому сайту,
    которому он нужнее (минимум максимума), а второму — второй лучший.
    """
    site_a = SiteConfig(name="siteA", url_template="https://a.test", phone="auto")
    site_b = SiteConfig(name="siteB", url_template="https://b.test", phone="auto")
    sites = [site_a, site_b]
    settings = Settings(sites=sites)

    # siteA: phone_best=10.0, phone_second=15.0
    # siteB: phone_best=12.0, phone_second=25.0
    # Если siteA -> best, siteB -> second: max = 25.0
    # Если siteB -> best, siteA -> second: max = 15.0 (оптимум)
    stats = {
        ("siteA", "phone_best"): FakeSpeedStats(cost=10.0, n=10, p_lower=0.9),
        ("siteA", "phone_second"): FakeSpeedStats(cost=15.0, n=10, p_lower=0.9),
        ("siteB", "phone_best"): FakeSpeedStats(cost=12.0, n=10, p_lower=0.9),
        ("siteB", "phone_second"): FakeSpeedStats(cost=25.0, n=10, p_lower=0.9),
    }
    speed_tbl = FakeSpeedTable(stats)

    def candidates_fn(s_obj, site, priority=0):
        return ["phone_best", "phone_second"]

    screen_state = {"phone_best": True, "phone_second": True}

    res = plan_live_assignment(
        settings,
        sites,
        speed_table=speed_tbl,
        screen_state=screen_state,
        candidates_fn=candidates_fn,
    )

    assert res["siteB"][0] == "phone_best"
    assert res["siteA"][0] == "phone_second"
    assert res["siteB"] == ["phone_best", "phone_second"]
    assert res["siteA"] == ["phone_second", "phone_best"]


def test_wake_penalty_shifts_assignment():
    """(б) Поправка на пробуждение переносит назначение на включённый экран,
    если выигрыш больше разницы стоимостей.
    """
    site_a = SiteConfig(name="siteA", url_template="https://a.test", phone="auto")
    settings = Settings(sites=[site_a], phone_wake_seconds=5.0)

    # phone_sleeping базово быстрее (10.0 против 12.0), но экран выключен (+5.0 сек -> 15.0)
    stats = {
        ("siteA", "phone_sleeping"): FakeSpeedStats(cost=10.0, n=10, p_lower=0.9),
        ("siteA", "phone_awake"): FakeSpeedStats(cost=12.0, n=10, p_lower=0.9),
    }
    speed_tbl = FakeSpeedTable(stats)

    def candidates_fn(s_obj, site, priority=0):
        return ["phone_sleeping", "phone_awake"]

    # При спящем экране phone_sleeping становится 15.0 > 12.0, выбирается phone_awake
    screen_state = {"phone_sleeping": False, "phone_awake": True}
    res = plan_live_assignment(
        settings,
        [site_a],
        speed_table=speed_tbl,
        screen_state=screen_state,
        candidates_fn=candidates_fn,
    )
    assert res["siteA"][0] == "phone_awake"
    assert res["siteA"] == ["phone_awake", "phone_sleeping"]

    # Если состояние экрана неизвестно (None), поправка не добавляется (10.0 < 12.0)
    screen_state_unknown = {"phone_sleeping": None, "phone_awake": True}
    res_unknown = plan_live_assignment(
        settings,
        [site_a],
        speed_table=speed_tbl,
        screen_state=screen_state_unknown,
        candidates_fn=candidates_fn,
    )
    assert res_unknown["siteA"][0] == "phone_sleeping"
    assert res_unknown["siteA"] == ["phone_sleeping", "phone_awake"]


def test_low_p_lower_not_primary():
    """(в) Пара с низким p_lower не становится основной."""
    site_a = SiteConfig(name="siteA", url_template="https://a.test", phone="auto")
    site_b = SiteConfig(name="siteB", url_template="https://b.test", phone="auto")
    settings = Settings(sites=[site_a, site_b], speed_live_min_p_lower=0.6)

    # siteA: dev_unreliable быстрее по времени (5.0), но p_lower=0.3 (<0.6).
    # Основным должен стать dev_reliable (cost 15.0, p_lower 0.9).
    stats = {
        ("siteA", "dev_unreliable"): FakeSpeedStats(cost=5.0, n=10, p_lower=0.3),
        ("siteA", "dev_reliable"): FakeSpeedStats(cost=15.0, n=10, p_lower=0.9),
        ("siteB", "dev_unreliable"): FakeSpeedStats(cost=5.0, n=10, p_lower=0.3),
    }
    speed_tbl = FakeSpeedTable(stats)

    def candidates_fn(s_obj, site, priority=0):
        if site.name == "siteA":
            return ["dev_unreliable", "dev_reliable"]
        return ["dev_unreliable"]

    res = plan_live_assignment(
        settings,
        [site_a, site_b],
        speed_table=speed_tbl,
        screen_state={"dev_unreliable": True, "dev_reliable": True},
        candidates_fn=candidates_fn,
    )

    # dev_unreliable уходит в запасные в конец списка siteA
    assert res["siteA"][0] == "dev_reliable"
    assert res["siteA"] == ["dev_reliable", "dev_unreliable"]

    # siteB не имеет ни одного основного кандидата: сохраняет свой прежний список
    assert res["siteB"] == ["dev_unreliable"]


def test_more_sites_than_phones_repeats_without_error():
    """(г) Сайтов больше телефонов: телефоны повторяются без падения."""
    sites = [
        SiteConfig(name=f"site_{i}", url_template=f"https://{i}.test", phone="auto")
        for i in range(5)
    ]
    settings = Settings(sites=sites)

    # 5 сайтов и всего 2 телефона: dev1 и dev2
    stats = {}
    for i in range(5):
        stats[(f"site_{i}", "dev1")] = FakeSpeedStats(cost=10.0 + i, n=10, p_lower=0.9)
        stats[(f"site_{i}", "dev2")] = FakeSpeedStats(cost=12.0 + i, n=10, p_lower=0.9)
    speed_tbl = FakeSpeedTable(stats)

    def candidates_fn(s_obj, site, priority=0):
        return ["dev1", "dev2"]

    res = plan_live_assignment(
        settings,
        sites,
        speed_table=speed_tbl,
        screen_state={"dev1": True, "dev2": True},
        candidates_fn=candidates_fn,
    )

    assert len(res) == 5
    assigned = [res[s.name][0] for s in sites]
    # Оба телефона задействованы
    assert "dev1" in assigned
    assert "dev2" in assigned
    # Телефоны повторяются
    assert len(set(assigned)) < len(sites)
    for s in sites:
        assert len(res[s.name]) == 2


@pytest.mark.asyncio
async def test_live_assignment_off_or_error_does_not_break_search(monkeypatch):
    """(д) LIVE_ASSIGNMENT=0 / ошибка в раскладке не ломает поиск (pipeline.search)."""
    site = SiteConfig("site1", "https://site.test/{origin}{destination}", phone="dev1", parser="test_parser")
    settings = Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="secret_token", live_assignment=False)
    q = Query("MOW", "ALA", date(2026, 12, 10))

    async def fake_fetch_page_snapshot_any(settings, candidates, url, **kwargs):
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = candidates[0]
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='[{"price": 5000}]'), True

    def dummy_parser(snap, q):
        from avia.models import Offer
        return [Offer(site="site1", price=5000, airline="SU")]

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)
    from avia.parsers import PARSERS
    monkeypatch.setitem(PARSERS, "test_parser", dummy_parser)

    # 1. LIVE_ASSIGNMENT=0 (settings.live_assignment=False): поиск работает без сбоев
    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is True
    assert len(results[0].offers) == 1

    # 2. Ошибка в раскладке plan_live_assignment логируется как warning и не ломает поиск
    settings.live_assignment = True

    def broken_dispatcher(*args, **kwargs):
        raise RuntimeError("Ошибка в алгоритме раскладки")

    monkeypatch.setattr("avia.dispatcher.plan_live_assignment", broken_dispatcher)

    results2 = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results2) == 1
    assert results2[0].ok is True
    assert len(results2[0].offers) == 1


@pytest.mark.asyncio
async def test_from_browser_device_order_reordering(monkeypatch):
    """Проверка, что device_order переупорядочивает remaining_cands в _from_browser."""
    tried_order = []

    async def fake_fetch_page_snapshot_any(settings, candidates, url, **kwargs):
        dev = candidates[0]
        tried_order.append(dev)
        stats = kwargs.get("stats")
        if stats is not None:
            stats["device"] = dev
            stats["fetch_seconds"] = 1.0
        return Snapshot(url=url, text='[{"price": 4000}]'), True

    def dummy_parser(snap, q):
        from avia.models import Offer
        return [Offer(site="site1", price=4000, airline="SU")]

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)
    from avia.parsers import PARSERS
    monkeypatch.setitem(PARSERS, "test_parser", dummy_parser)

    site = SiteConfig("site1", "https://site.test/{origin}{destination}", phone="dev1,dev2,dev3", parser="test_parser")
    settings = Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="secret_token")
    q = Query("MOW", "ALA", date(2026, 12, 10))

    # Передаём device_order, где dev2 первым
    await _from_browser(site, q, settings, priority=0, device_order=["dev2", "dev1", "dev3"])
    assert tried_order[0] == "dev2"

    # Для priority > 0 device_order игнорируется
    tried_order.clear()
    await _from_browser(site, q, settings, priority=1, device_order=["dev2", "dev1", "dev3"])
    assert tried_order[0] == "dev1"


def test_quarantine_candidates_go_to_tail(tmp_path):
    """Карантинные устройства уходят в самый конец списка после основных и запасных."""
    site = SiteConfig(name="siteA", url_template="https://a.test", phone="auto")
    settings = Settings(sites=[site])

    stats = {
        ("siteA", "dev_primary"): FakeSpeedStats(cost=20.0, n=10, p_lower=0.9),
        ("siteA", "dev_backup"): FakeSpeedStats(cost=10.0, n=2, p_lower=0.5),
        ("siteA", "dev_quar"): FakeSpeedStats(cost=5.0, n=10, p_lower=0.9),
    }
    pairs = {
        ("siteA", "dev_quar"): FakePairStatus(in_quarantine=True),
    }
    speed_tbl = FakeSpeedTable(stats, pairs)

    def candidates_fn(s_obj, site, priority=0):
        return ["dev_quar", "dev_backup", "dev_primary"]

    res = plan_live_assignment(
        settings,
        [site],
        speed_table=speed_tbl,
        screen_state={"dev_primary": True, "dev_backup": True, "dev_quar": True},
        candidates_fn=candidates_fn,
    )

    # dev_primary основной, dev_backup запасной, dev_quar в хвосте
    assert res["siteA"] == ["dev_primary", "dev_backup", "dev_quar"]


def test_default_screen_state_from_registry(tmp_path):
    """screen_state по умолчанию извлекается из PhoneRegistry."""
    phones_file = tmp_path / "phones.json"
    reg = PhoneRegistry(path=phones_file)
    reg.register("dev1", "Dev 1", status="active")
    reg.register("dev2", "Dev 2", status="active")
    reg.update_snapshot("dev1", screen_on=True)
    reg.update_snapshot("dev2", screen_on=False)

    site = SiteConfig(name="siteA", url_template="https://a.test", phone="auto")
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg, phone_wake_seconds=10.0)

    # dev2 базово 15.0, но экран False -> 15 + 10 = 25.0
    # dev1 базово 18.0, экран True -> 18.0
    stats = {
        ("siteA", "dev1"): FakeSpeedStats(cost=18.0, n=10, p_lower=0.9),
        ("siteA", "dev2"): FakeSpeedStats(cost=15.0, n=10, p_lower=0.9),
    }
    speed_tbl = FakeSpeedTable(stats)

    def candidates_fn(s_obj, site, priority=0):
        return ["dev1", "dev2"]

    res = plan_live_assignment(
        settings,
        [site],
        speed_table=speed_tbl,
        candidates_fn=candidates_fn,
    )
    # dev1 должен быть первым, так как dev2 получил штраф на пробуждение
    assert res["siteA"][0] == "dev1"
