"""Тесты терпеливого предпочтения телефона и полных параметров ожидания Яндекса."""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.droidhub import (
    PreferenceList,
    _busy_devices,
    _dispatch,
    _reset_pool,
    _time,
    _waiters,
    acquire,
    advance_time,
    candidates,
    fetch_page_snapshot,
    release,
    set_clock,
)
from avia.models import Query, SiteResult
from avia.pipeline import _from_browser, _run_site


class FakeClock:
    """Управляемые часы для тестов с предсказуемым временем."""

    def __init__(self, current: float = 1000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, dt: float) -> None:
        self.current += dt


@pytest.fixture(autouse=True)
def clean_pool():
    _reset_pool()
    yield
    _reset_pool()


# ---------------------------------------------------------------------------
# 1. Загрузка конфигурации из YAML и валидация SiteConfig
# ---------------------------------------------------------------------------


def test_yaml_config_loading():
    """Проверка загрузки настроек phone_patience_seconds и phone_full_* из sites.yaml."""
    settings = load_settings()
    by_name = {s.name: s for s in settings.sites}

    assert by_name["kupibilet"].phone_patience_seconds == 15
    assert by_name["aviasales"].phone_patience_seconds == 10
    assert by_name["ozon_travel"].phone_patience_seconds == 10
    assert by_name["tutu"].phone_patience_seconds == 5
    assert by_name["onetwotrip"].phone_patience_seconds == 5

    ya = by_name["yandex_travel"]
    assert ya.phone == "realme,redmi"
    assert ya.phone_patience_seconds == 5
    assert ya.phone_wait_seconds == 30
    assert ya.phone_full_wait_seconds == 45
    assert ya.phone_full_wait_for == '"cont":null'

    # Проверка кандидатов для Яндекса
    cands = candidates(settings, ya)
    assert isinstance(cands, PreferenceList)
    assert list(cands) == ["realme", "redmi"]
    assert cands.patience == 5.0


def test_site_config_validation():
    """SiteConfig проверяет, что phone_patience_seconds >= 0."""
    with pytest.raises(ValueError, match="phone_patience_seconds must be >= 0"):
        SiteConfig("bad_site", "https://site.test", phone_patience_seconds=-1)

    s = SiteConfig("ok_site", "https://site.test", phone_patience_seconds=0)
    assert s.phone_patience_seconds == 0
    assert s.phone_full_wait_seconds is None
    assert s.phone_full_wait_for is None


# ---------------------------------------------------------------------------
# 2. Терпение = 0: прежнее поведение
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patience_zero_takes_fallback_immediately():
    """При patience=0 диспетчер сразу берёт запасной, если предпочтительный занят."""
    # Занимаем предпочтительный телефон
    await acquire(["realme"])
    assert "realme" in _busy_devices

    pref = PreferenceList(["realme", "redmi"], patience=0)
    dev = await acquire(pref)
    # Сразу взят запасной телефон без ожидания
    assert dev == "redmi"


# ---------------------------------------------------------------------------
# 3. Терпение = N: ждёт предпочтительный, берёт запасной после N секунд
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patience_waits_for_preferred_then_takes_fallback_fake_clock():
    """При patience=N ждёт N секунд, и если предпочтительный занят, берёт запасной."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # Занимаем realme
    await acquire(["realme"])

    pref = PreferenceList(["realme", "redmi"], patience=10.0)
    task = asyncio.create_task(acquire(pref))
    await asyncio.sleep(0.001)

    # Задача ждёт в очереди, не забирает запасной
    assert not task.done()
    assert "redmi" not in _busy_devices

    # Прошло 5 секунд (половина терпения)
    advance_time(5.0)
    await asyncio.sleep(0.001)
    assert not task.done()
    assert "redmi" not in _busy_devices

    # Прошло ещё 5 секунд (дедлайн наступил)
    advance_time(5.0)
    await asyncio.sleep(0.001)

    assert task.done()
    assert task.result() == "redmi"


# ---------------------------------------------------------------------------
# 4. Терпение = N: берёт предпочтительный, если освободился раньше
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patience_takes_preferred_if_released_earlier():
    """Если предпочтительный освободился раньше N секунд, берётся именно он."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # Занимаем realme
    d_hold = await acquire(["realme"])

    pref = PreferenceList(["realme", "redmi"], patience=15.0)
    task = asyncio.create_task(acquire(pref))
    await asyncio.sleep(0.001)

    assert not task.done()

    # Прошло 3 секунды
    advance_time(3.0)
    assert not task.done()

    # Предпочтительный освободился до истечения 15 секунд
    await release(d_hold)
    await asyncio.sleep(0.001)

    assert task.done()
    assert task.result() == "realme"
    # Запасной телефон остался свободным
    assert "redmi" not in _busy_devices


# ---------------------------------------------------------------------------
# 5. Отмена ждущего не оставляет мусора и таймеров
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_waiter_cleans_up():
    """Отмена ожидающего корректно убирает waiter из очереди и отменяет таймер."""
    clock = FakeClock(100.0)
    set_clock(clock)

    d_hold = await acquire(["realme"])

    pref = PreferenceList(["realme", "redmi"], patience=10.0)
    task = asyncio.create_task(acquire(pref))
    await asyncio.sleep(0.001)

    assert len(_waiters) == 1
    w = _waiters[0]
    assert w.timer_handle is not None

    # Отменяем задачу
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # В очереди ничего не осталось, таймер отменён
    assert len(_waiters) == 0
    assert "redmi" not in _busy_devices

    await release(d_hold)
    assert len(_busy_devices) == 0


# ---------------------------------------------------------------------------
# 6. Терпение не блокирует других и сохраняет приоритеты
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patience_does_not_block_other_waiters():
    """Ждущий в терпении не занимает запасной телефон и не мешает другим его взять."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # Занимаем realme
    await acquire(["realme"])

    # Запрос 1: высокий приоритет 0, терпение 10 к realme
    pref1 = PreferenceList(["realme", "redmi"], patience=10.0)
    t1 = asyncio.create_task(acquire(pref1, priority=0))
    await asyncio.sleep(0.001)

    # Запрос 2: подписка (priority 1), которой нужен redmi
    t2 = asyncio.create_task(acquire(["redmi"], priority=1))
    await asyncio.sleep(0.001)

    # Запрос 2 должен получить redmi, несмотря на то, что запрос 1 в очереди раньше
    assert t2.done()
    assert t2.result() == "redmi"

    # Запрос 1 всё ещё терпеливо ждёт realme
    assert not t1.done()


@pytest.mark.asyncio
async def test_priorities_preserved_with_patience():
    """Приоритеты (priority, -weight, seq) строго сохраняются при распределении."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # Занимаем оба телефона
    d_realme = await acquire(["realme"])
    d_redmi = await acquire(["redmi"])

    results: list[tuple[str, str]] = []

    async def runner(tag: str, pref: PreferenceList, prio: int, weight: int):
        dev = await acquire(pref, priority=prio, weight=weight)
        results.append((tag, dev))
        await release(dev)

    # Человек (priority=0, weight=10, patience=5)
    t_human = asyncio.create_task(runner("human", PreferenceList(["realme", "redmi"], patience=5.0), 0, 10))
    # Подписка (priority=1, weight=50, patience=0)
    t_sub = asyncio.create_task(runner("sub", PreferenceList(["realme", "redmi"], patience=0.0), 1, 50))
    # Фон (priority=2, weight=100, patience=0)
    t_bg = asyncio.create_task(runner("bg", PreferenceList(["realme", "redmi"], patience=0.0), 2, 100))
    await asyncio.sleep(0.001)

    # Освобождаем realme: должен достаться человеку (priority 0)
    await release(d_realme)
    await asyncio.sleep(0.001)

    assert results[0][0] == "human"
    assert results[0][1] == "realme"

    # Освобождаем redmi: должен достаться подписке (priority 1 обгоняет фон 2)
    await release(d_redmi)
    await asyncio.gather(t_sub, t_bg)

    assert results[1][0] == "sub"
    assert results[2][0] == "bg"


# ---------------------------------------------------------------------------
# 7. Яндекс Путешествия: быстрый режим для живого поиска, полный для подписок/фона
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yandex_wait_seconds_and_wait_for_by_priority(monkeypatch):
    """priority 0 использует быстрые секунды без wait_for; priority 1 и 2 — полные."""
    settings = load_settings()
    settings.droidhub_url = "https://hub.test"
    settings.droidhub_token = "secret"
    ya_site = next(s for s in settings.sites if s.name == "yandex_travel")

    captured_args: list[dict] = []

    async def fake_fetch_page_snapshot_any(settings, candidates, url, **kwargs):
        captured_args.append(kwargs)
        return Snapshot(url=url, text='{"cont":null}', json_payloads=[]), True

    q = Query("MOW", "LED", date(2026, 12, 1))

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_page_snapshot_any)
    monkeypatch.setattr("avia.pipeline.resolve_extra", AsyncMock(return_value={"origin_id": "c213", "destination_id": "c2"}))
    monkeypatch.setitem(
        __import__("avia.parsers", fromlist=["PARSERS"]).PARSERS,
        "yandex_travel",
        lambda snap, q: [__import__("avia.models", fromlist=["Offer"]).Offer(site="yandex_travel", price=5000, depart_at=q.depart.isoformat())],
    )

    guard = __import__("avia.guard", fromlist=["SearchGuard"]).SearchGuard(cache_ttl=0, min_interval=0)

    # priority 0 (живой поиск человека)
    res0 = await _run_site(ya_site, q, settings, priority=0, guard=guard)
    assert res0.ok is True
    assert captured_args[0]["wait_seconds"] == 30
    assert captured_args[0]["wait_for"] == ""
    assert captured_args[0]["weight"] == 30

    # priority 1 (подписка)
    res1 = await _run_site(ya_site, q, settings, priority=1, guard=guard)
    assert res1.ok is True
    assert captured_args[1]["wait_seconds"] == 45
    assert captured_args[1]["wait_for"] == '"cont":null'
    assert captured_args[1]["weight"] == 45

    # priority 2 (фон)
    res2 = await _run_site(ya_site, q, settings, priority=2, guard=guard)
    assert res2.ok is True
    assert captured_args[2]["wait_seconds"] == 45
    assert captured_args[2]["wait_for"] == '"cont":null'
    assert captured_args[2]["weight"] == 45


# ---------------------------------------------------------------------------
# 8. Реальный таймер с небольшим интервалом (asyncio call_later)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_loop_timer_patience():
    """Тест реального таймера asyncio loop.call_later на коротком интервале."""
    # Занимаем realme
    d_hold = await acquire(["realme"])

    # Терпение 0.05 с (50 мс)
    pref = PreferenceList(["realme", "redmi"], patience=0.05)
    t = asyncio.create_task(acquire(pref))
    await asyncio.sleep(0.01)

    assert not t.done()
    assert "redmi" not in _busy_devices

    # Ждём 60 мс
    await asyncio.sleep(0.06)
    assert t.done()
    assert t.result() == "redmi"

    await release(d_hold)
    await release("redmi")


# ---------------------------------------------------------------------------
# 9. Дополнительные тесты: допуск времени, перезавод таймера, PreferenceList в fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patience_float_tolerance_and_timer_rearm():
    """Если таймер сработал с микросекундной погрешностью (< 1e-4), дедлайн считается наступившим."""
    clock = FakeClock(100.0)
    set_clock(clock)

    # Занимаем предпочтительный realme
    d_hold = await acquire(["realme"])

    # Запрос с терпением 5 секунд
    pref = PreferenceList(["realme", "redmi"], patience=5.0)
    task = asyncio.create_task(acquire(pref))
    await asyncio.sleep(0.001)

    assert len(_waiters) == 1
    w = _waiters[0]
    assert w.timer_handle is not None

    # Имитируем раннее срабатывание таймера на 4.99995 секунды (разница 5e-5 < 1e-4)
    # По логике допуска дедлайн уже считается наступившим, и берётся запасной
    advance_time(5.0 - 5e-5)
    _dispatch()
    await asyncio.sleep(0.001)

    assert task.done()
    assert task.result() == "redmi"

    await release(d_hold)
    await release("redmi")


@pytest.mark.asyncio
async def test_patience_timer_rearm_if_early():
    """Если таймер терпения был сброшен раньше времени (now < deadline), _dispatch перезаводит его."""
    clock = FakeClock(100.0)
    set_clock(clock)

    d_hold = await acquire(["realme"])
    pref = PreferenceList(["realme", "redmi"], patience=5.0)
    task = asyncio.create_task(acquire(pref))
    await asyncio.sleep(0.001)

    w = _waiters[0]
    # Имитируем сброс таймера на 3-й секунде
    advance_time(3.0)
    if w.timer_handle:
        w.timer_handle.cancel()
    w.timer_handle = None

    _dispatch()
    # Таймер должен быть гарантированно перезаведён
    assert w.timer_handle is not None
    assert not task.done()

    # Продвигаем время ещё на 2 секунды (истечение дедлайна)
    advance_time(2.0)
    await asyncio.sleep(0.001)
    assert task.done()
    assert task.result() == "redmi"

    await release(d_hold)
    await release("redmi")


@pytest.mark.asyncio
async def test_fetch_page_snapshot_preserves_preferencelist(monkeypatch):
    """fetch_page_snapshot сохраняет объект PreferenceList и его атрибуты prefer_order и patience."""
    captured = {}

    async def fake_any(settings, devs, url, **kwargs):
        captured["devs"] = devs
        captured["kwargs"] = kwargs
        return Snapshot(url=url, text="ok", json_payloads=[]), True

    monkeypatch.setattr("avia.droidhub.fetch_page_snapshot_any", fake_any)

    settings = Settings(sites=[])
    pref = PreferenceList(["realme", "redmi"], patience=7.5)

    snap, fin = await fetch_page_snapshot(settings, pref, "https://example.com")
    assert snap.text == "ok"
    assert fin is True

    # Проверяем, что devs сохранил тип PreferenceList
    assert isinstance(captured["devs"], PreferenceList)
    assert captured["devs"].prefer_order is True
    assert captured["devs"].patience == 7.5
    # И kwargs тоже содержат prefer_order и patience
    assert captured["kwargs"]["prefer_order"] is True
    assert captured["kwargs"]["patience"] == 7.5


def test_site_config_from_avia_config():
    """Проверка создания и валидации SiteConfig напрямую через импорт из avia.config."""
    from avia.config import SiteConfig as CfgSiteConfig

    s = CfgSiteConfig(
        name="test",
        url_template="https://test.com",
        phone_patience_seconds=12,
        phone_full_wait_seconds=50,
        phone_full_wait_for='"done":true',
    )
    assert s.phone_patience_seconds == 12
    assert s.phone_full_wait_seconds == 50
    assert s.phone_full_wait_for == '"done":true'

    with pytest.raises(ValueError, match="phone_patience_seconds must be >= 0"):
        CfgSiteConfig(name="test", url_template="https://test.com", phone_patience_seconds=-5)
