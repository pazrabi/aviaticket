"""Тесты для правила повтора живого поиска на следующем телефоне (шаг 6).

Повтор на следующем телефоне разрешён, если:
    «прошло с начала поиска + ожидаемое время (медиана) следующего кандидата» <= live_safety_seconds.
Если search_started не передан, используется прежнее поведение _hit_time_cap.
Фон и подписки (priority != 0) не ограничиваются предохранителем.
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import time
from typing import Any

import pytest

from avia.config import Settings, SiteConfig
from avia.guard import SearchGuard
from avia.models import Offer, Query, SiteResult
from avia.pipeline import ParserEmptyError, _can_retry_on_phone, search
from avia.speed import reset_speed_tables


@pytest.fixture(autouse=True)
def _reset_speeds():
    reset_speed_tables()
    yield
    reset_speed_tables()


def make_settings(tmp_path: Path, safety_seconds: float = 90.0, wake_seconds: float = 5.0) -> Settings:
    return Settings(
        sites=[SiteConfig(name="testsite", url_template="http://testsite", phone_wait_seconds=20)],
        tg_token="dummy",
        storage_dir=tmp_path,
        live_safety_seconds=safety_seconds,
        phone_wake_seconds=wake_seconds,
    )


def test_retry_allowed_with_time_margin(tmp_path):
    """Повтор разрешён, если с учётом ожидаемого времени укладываемся в предохранитель."""
    settings = make_settings(tmp_path, safety_seconds=90.0)
    # Нет данных в таблице скоростей -> ожидаемое = phone_wake (5) + wait (20) = 25 с
    # Прошло: 120 - 100 = 20 с. 20 + 25 = 45 <= 90 -> разрешён
    can_retry = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 20.0},
        wait_seconds=20,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=120.0,
    )
    assert can_retry is True


def test_retry_forbidden_when_time_exceeded(tmp_path):
    """Повтор запрещён, если «прошло + медиана» превышает live_safety_seconds."""
    settings = make_settings(tmp_path, safety_seconds=90.0)
    # Ожидаемое: 5 + 20 = 25 с. Прошло: 170 - 100 = 70 с. 70 + 25 = 95 > 90 -> запрещён
    can_retry = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 20.0},
        wait_seconds=20,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=170.0,
    )
    assert can_retry is False


def test_without_search_started_uses_old_hit_time_cap(tmp_path):
    """Без search_started (None) логика остаётся прежней: _hit_time_cap."""
    settings = make_settings(tmp_path)

    # 1. fetch_seconds >= wait_seconds * 0.8 (40 >= 45 * 0.8 = 36) -> старый cap сработал, повтор запрещён
    res_slow = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 40.0, "finished": False},
        wait_seconds=45,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=None,
    )
    assert res_slow is False

    # 2. fetch_seconds мал -> повтор разрешён
    res_fast = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 10.0, "finished": False},
        wait_seconds=45,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=None,
    )
    assert res_fast is True

    # 3. Страница дозагрузилась (finished=True), разбор 0 -> повтор разрешён
    res_finished = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 40.0, "finished": True},
        wait_seconds=45,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=None,
    )
    assert res_finished is True


def test_speed_table_expected_cost_used(tmp_path):
    """Ожидаемое время берётся из SpeedTable (cost/медиана), когда оно известно."""
    from avia.metrics import MetricsStore

    metrics_file = tmp_path / "metrics.sqlite"
    store = MetricsStore(metrics_file)

    # Записываем быстрые замеры для пары testsite + phone_fast
    now_ts = time.time()
    for _ in range(6):
        store.record_run(
            source="user",
            site="testsite",
            ok=True,
            partial=False,
            captcha=False,
            error=None,
            offers=5,
            elapsed=8.0,
            fetch_seconds=8.0,
            device="phone_fast",
            priority=0,
            ts=now_ts,
        )

    settings = make_settings(tmp_path, safety_seconds=50.0)

    # Таблица отдаст ожидаемое время около 8 с
    # Прошло: 135 - 100 = 35 с. 35 + 8 = 43 <= 50 -> разрешён
    res1 = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 10.0},
        wait_seconds=30,  # без таблицы было бы 5 + 30 = 35 -> 35 + 35 = 70 > 50 (запрет)
        remaining_cands=["phone_fast"],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=135.0,
    )
    assert res1 is True

    # Прошло: 145 - 100 = 45 с. 45 + 8 = 53 > 50 -> запрещён
    res2 = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 10.0},
        wait_seconds=30,
        remaining_cands=["phone_fast"],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=145.0,
    )
    assert res2 is False


def test_bg_priority_not_limited_by_safety_cap(tmp_path):
    """Фон и подписки (priority != 0) не ограничиваются live_safety_seconds."""
    settings = make_settings(tmp_path, safety_seconds=10.0)
    # Прошло 500 с, намного больше 10 с предохранителя
    can_retry = _can_retry_on_phone(
        priority=1,
        cur_stats={"fetch_seconds": 20.0},
        wait_seconds=20,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=600.0,
    )
    assert can_retry is True


def test_no_remaining_candidates_returns_false(tmp_path):
    """Если кандидатов не осталось, повтор невозможен."""
    settings = make_settings(tmp_path)
    can_retry = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 5.0},
        wait_seconds=20,
        remaining_cands=[],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=105.0,
    )
    assert can_retry is False


def test_live_safety_seconds_zero_disables_cap(tmp_path):
    """При live_safety_seconds=0 ограничение отключено."""
    settings = make_settings(tmp_path, safety_seconds=0.0)
    can_retry = _can_retry_on_phone(
        priority=0,
        cur_stats={"fetch_seconds": 20.0},
        wait_seconds=20,
        remaining_cands=["phone2"],
        settings=settings,
        site_name="testsite",
        search_started=100.0,
        now=500.0,
    )
    assert can_retry is True


@pytest.mark.asyncio
async def test_pipeline_retry_under_safety_cap(tmp_path, monkeypatch):
    """Сквозной тест через pipeline: первый телефон даёт ParserEmptyError,

    при достаточном запасе времени пробуется второй телефон.
    """
    site = SiteConfig(
        name="test_site",
        url_template="http://test/{origin}",
        phone="phone_a, phone_b",
        phone_wait_seconds=10,
    )
    settings = Settings(
        sites=[site],
        tg_token="dummy",
        droidhub_url="http://fake-droidhub",
        droidhub_token="dummy_token",
        storage_dir=tmp_path,
        live_safety_seconds=90.0,
        phone_wake_seconds=2.0,
        speed_routing=False,
    )

    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    attempts: list[str] = []

    async def fake_fetch_snapshot_any(settings, candidates, url, **kwargs):
        from avia.collector import Snapshot

        # candidates - список или PreferenceList
        dev = candidates[0]
        attempts.append(dev)
        if "stats" in kwargs and kwargs["stats"] is not None:
            kwargs["stats"]["device"] = dev
            kwargs["stats"]["fetch_seconds"] = 1.0
        if dev == "phone_a":
            # Возвращаем снапшот, на котором парсер выдаст пустой результат
            return Snapshot(url=url, text="<html></html>"), True
        return Snapshot(url=url, text='<html><div class="ticket">6000</div></html>'), True

    async def fake_process_snapshot(site, snap, q, settings, partial=False):
        if "ticket" not in snap.text:
            raise ParserEmptyError("пусто", page_url="http://test")
        return [Offer(site.name, 6000)], "http://test", False

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_snapshot_any)
    monkeypatch.setattr("avia.pipeline._process_snapshot", fake_process_snapshot)

    results = await search(q, settings, guard=SearchGuard())
    assert len(results) == 1
    assert results[0].ok is True
    assert len(results[0].offers) == 1
    assert attempts == ["phone_a", "phone_b"], "Должен был повторить на phone_b"


@pytest.mark.asyncio
async def test_pipeline_no_retry_when_safety_cap_exceeded(tmp_path, monkeypatch):
    """Сквозной тест через pipeline: при превышении live_safety_seconds повтор не происходит."""
    site = SiteConfig(
        name="test_site",
        url_template="http://test/{origin}",
        phone="phone_a, phone_b",
        phone_wait_seconds=20,
    )
    # Предохранитель всего 10 с, а ожидание phone_b = wake(5) + wait(20) = 25 с > 10 с
    settings = Settings(
        sites=[site],
        tg_token="dummy",
        droidhub_url="http://fake-droidhub",
        droidhub_token="dummy_token",
        storage_dir=tmp_path,
        live_safety_seconds=10.0,
        phone_wake_seconds=5.0,
        speed_routing=False,
    )

    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    attempts: list[str] = []

    async def fake_fetch_snapshot_any(settings, candidates, url, **kwargs):
        from avia.collector import Snapshot

        dev = candidates[0]
        attempts.append(dev)
        if "stats" in kwargs and kwargs["stats"] is not None:
            kwargs["stats"]["device"] = dev
            kwargs["stats"]["fetch_seconds"] = 1.0
        return Snapshot(url=url, text="<html></html>"), True

    async def fake_process_snapshot(site, snap, q, settings, partial=False):
        raise ParserEmptyError("пусто", page_url="http://test")

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_snapshot_any)
    monkeypatch.setattr("avia.pipeline._process_snapshot", fake_process_snapshot)

    results = await search(q, settings, guard=SearchGuard())
    assert len(results) == 1
    assert results[0].ok is False
    assert attempts == ["phone_a"], "Не должен был пытаться phone_b из-за предохранителя"
