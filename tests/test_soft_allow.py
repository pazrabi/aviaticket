"""Тесты периодической мягкой проверки запрещённых пар сайт × телефон (soft sites_allow)."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import time
from typing import Any

import pytest
import yaml

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.droidhub import DroidhubError
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.phone_check import _soft_allow_lock, probe_denied_pairs
from avia.phones import PhoneRegistry
from avia.speed import PairStatus, get_speed_table


class FakeBot:
    """Фейковый бот для тестов без сети."""

    def __init__(self, settings: Settings):
        from avia.bot import Bot

        if not settings.tg_token:
            settings.tg_token = "fake_tg_token"
        if not settings.tg_allowed_ids:
            settings.tg_allowed_ids = {12345}
        self.bot = Bot(settings)
        self.bot.call = self._mock_call  # type: ignore
        self.sent_messages: list[dict[str, Any]] = []

    async def _mock_call(self, method: str, **kwargs: Any) -> Any:
        if method in ("sendMessage", "editMessageText"):
            self.sent_messages.append(kwargs)
            return {"message_id": 100, **kwargs}
        return {}


def _create_sites_yaml(path: Path) -> Path:
    cfg = {
        "sites": [
            {
                "name": "site1",
                "url_template": "https://s1.test",
                "phone": "auto",
                "parser": "p_test",
                "source": "browser",
            },
            {
                "name": "site2",
                "url_template": "https://s2.test",
                "phone": "auto",
                "parser": "p_test",
                "source": "browser",
            },
        ]
    }
    yaml_path = path / "sites.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return yaml_path


# (а) пара вернулась при 2 из 3 → сайт добавлен в sites_allow и админам ушло сообщение
@pytest.mark.asyncio
async def test_soft_allow_success_2_of_3(tmp_path, monkeypatch):
    def parser_ok(snap, q):
        return [Offer(site="site1", price=5000)]

    parser_ok.results_complete = lambda snap: True
    PARSERS["p_test"] = parser_ok

    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_test")
    s2 = SiteConfig(name="site2", url_template="https://s2", phone="auto", parser="p_test")

    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Redmi 1", status="active", sites_allow=["site2"])
    reg.update_snapshot("dev1", online=True, battery=80)

    settings = Settings(
        sites=[s1, s2],
        storage_dir=tmp_path,
        tg_allowed_ids={12345},
        phones=reg,
    )
    fb = FakeBot(settings)

    call_count = 0

    async def mock_snapshot(s, device_id, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 1-й проход: успех
            return Snapshot(url=url, text="OK 1", blocked=False), True
        elif call_count == 2:
            # 2-й проход: сбой 502
            raise DroidhubError("502 Bad Gateway")
        else:
            # 3-й проход: успех
            return Snapshot(url=url, text="OK 2", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    res = await probe_denied_pairs(settings, bot=fb.bot)
    assert len(res) == 1
    assert res[0] == ("site1", "dev1", True)
    assert call_count == 3  # сделано 3 прохода (1 успех, 1 сбой, 1 успех)

    # Реестр обновился: site1 добавлен в sites_allow
    entry = reg.get("dev1")
    assert entry is not None
    assert "site1" in entry["sites_allow"]
    assert "site2" in entry["sites_allow"]
    assert "Допущен: site1 (авто" in entry["note"]

    # Оповещение администраторам
    assert len(fb.sent_messages) == 1
    sent = fb.sent_messages[0]
    assert sent["chat_id"] == 12345
    assert "✅ Телефон Redmi 1 снова допущен на site1" in sent["text"]


# (б) пара не вернулась → sites_allow не меняется, probe_at обновился
@pytest.mark.asyncio
async def test_soft_allow_failure_remains_denied_updates_probe_at(tmp_path, monkeypatch):
    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_test")
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Redmi 1", status="active", sites_allow=[])
    reg.update_snapshot("dev1", online=True, battery=80)

    settings = Settings(sites=[s1], storage_dir=tmp_path, tg_allowed_ids={12345}, phones=reg)
    fb = FakeBot(settings)

    call_count = 0

    async def mock_snapshot_fail(s, device_id, url, **kwargs):
        nonlocal call_count
        call_count += 1
        raise DroidhubError("502 Bad Gateway")

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot_fail)

    t0 = 1700000000.0
    res = await probe_denied_pairs(settings, bot=fb.bot, now=t0)
    assert len(res) == 1
    assert res[0] == ("site1", "dev1", False)
    assert call_count == 2  # прерывание после двух первых неудач подряд

    # sites_allow не изменился
    entry = reg.get("dev1")
    assert entry is not None
    assert entry["sites_allow"] == []
    # probe_at обновился
    assert reg.get_probe_at("dev1", "site1") == t0
    # Админам ничего не отправлялось
    assert len(fb.sent_messages) == 0


# (в) пара в кулдауне капчи не пробуется
@pytest.mark.asyncio
async def test_soft_allow_captcha_cooldown_skipped(tmp_path, monkeypatch):
    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_test")
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Redmi 1", status="active", sites_allow=[])
    reg.update_snapshot("dev1", online=True, battery=80)

    settings = Settings(sites=[s1], storage_dir=tmp_path, phones=reg)
    fb = FakeBot(settings)

    # Выставляем карантин капчи в SpeedTable
    st = get_speed_table(settings)
    st._last_refresh = time.monotonic()
    st._pairs[("site1", "dev1")] = PairStatus(
        in_quarantine=True,
        consecutive_captchas=2,
        last_failure_ts=time.time(),
    )

    fetch_called = False

    async def mock_fetch(*args, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return Snapshot(url="u", text="ok", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_fetch)

    res = await probe_denied_pairs(settings, bot=fb.bot)
    assert res == []
    assert not fetch_called
    assert reg.get("dev1")["sites_allow"] == []


# (г) интервал: повторная проба раньше soft_allow_interval_hours не делается
@pytest.mark.asyncio
async def test_soft_allow_interval_hours(tmp_path, monkeypatch):
    def parser_ok(snap, q):
        return [Offer(site="site1", price=5000)]

    parser_ok.results_complete = lambda snap: True
    PARSERS["p_test"] = parser_ok

    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_test")
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Redmi 1", status="active", sites_allow=[])
    reg.update_snapshot("dev1", online=True, battery=80)

    t0 = 1700000000.0
    # Устанавливаем время прошлой пробы — 2 часа назад при интервале 24 ч
    reg.set_probe_at("dev1", "site1", ts=t0 - 2 * 3600)

    settings = Settings(
        sites=[s1],
        storage_dir=tmp_path,
        soft_allow_interval_hours=24.0,
        phones=reg,
    )
    fb = FakeBot(settings)

    fetch_calls = 0

    async def mock_snapshot(s, device_id, url, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    # 1. Попытка в момент t0 (прошло только 2 часа): пропуск
    res1 = await probe_denied_pairs(settings, bot=fb.bot, now=t0)
    assert res1 == []
    assert fetch_calls == 0

    # 2. Попытка через 25 часов: пара пробуется
    res2 = await probe_denied_pairs(settings, bot=fb.bot, now=t0 + 25 * 3600)
    assert len(res2) == 1
    assert res2[0] == ("site1", "dev1", True)
    assert fetch_calls >= 2


# (д) телефон со статусом hold/new и телефон с sites_allow=None пропускаются
@pytest.mark.asyncio
async def test_soft_allow_skips_hold_new_and_unrestricted_phones(tmp_path, monkeypatch):
    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_test")
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("p_hold", name="Hold Phone", status="hold", sites_allow=[])
    reg.update_snapshot("p_hold", online=True)
    reg.register("p_new", name="New Phone", status="new", sites_allow=[])
    reg.update_snapshot("p_new", online=True)
    reg.register("p_all", name="All Allowed", status="active", sites_allow=None)
    reg.update_snapshot("p_all", online=True)
    reg.register("p_offline", name="Offline Phone", status="active", sites_allow=[])
    reg.update_snapshot("p_offline", online=False)
    reg.register("p_no_snap", name="No Snap", status="active", sites_allow=[])
    reg.register("p_stale", name="Stale Snap", status="active", sites_allow=[])
    reg.update_snapshot("p_stale", online=True, updated_at=time.monotonic() - 300)

    settings = Settings(sites=[s1], storage_dir=tmp_path, phones=reg)
    fb = FakeBot(settings)

    fetch_called = False

    async def mock_fetch(*args, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return Snapshot(url="u", text="ok", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_fetch)

    res = await probe_denied_pairs(settings, bot=fb.bot)
    assert res == []
    assert not fetch_called


# (е) не больше max_pairs за запуск
@pytest.mark.asyncio
async def test_soft_allow_max_pairs_per_run_limit(tmp_path, monkeypatch):
    def parser_ok(snap, q):
        return [Offer(site="site1", price=5000)]

    parser_ok.results_complete = lambda snap: True
    PARSERS["p_test"] = parser_ok

    sites = [
        SiteConfig(name=f"site_{i}", url_template=f"https://s{i}", phone="auto", parser="p_test")
        for i in range(1, 6)
    ]
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Dev 1", status="active", sites_allow=[])
    reg.update_snapshot("dev1", online=True)

    settings = Settings(
        sites=sites,
        storage_dir=tmp_path,
        soft_allow_max_pairs_per_run=2,
        phones=reg,
    )
    fb = FakeBot(settings)

    async def mock_snapshot(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    # 1. По умолчанию берется settings.soft_allow_max_pairs_per_run == 2
    res = await probe_denied_pairs(settings, bot=fb.bot)
    assert len(res) == 2

    # 2. Явный аргумент max_pairs=1
    res1 = await probe_denied_pairs(settings, bot=fb.bot, max_pairs=1)
    assert len(res1) == 1


# (ж) SOFT_ALLOW=0 отключает вызов в цикле (проверь через настройку)
@pytest.mark.asyncio
async def test_soft_allow_disabled_setting_and_maintenance_loop(tmp_path, monkeypatch):
    yaml_path = _create_sites_yaml(tmp_path)

    # 1. Проверка парсинга настроек из окружения
    monkeypatch.setenv("SOFT_ALLOW", "0")
    monkeypatch.setenv("SOFT_ALLOW_INTERVAL_HOURS", "12")
    monkeypatch.setenv("SOFT_ALLOW_MAX_PAIRS", "4")

    s = load_settings(yaml_path)
    assert s.soft_allow_enabled is False
    assert s.soft_allow_interval_hours == 12.0
    assert s.soft_allow_max_pairs_per_run == 4

    # 2. Проверка работы в _maintenance_loop: при soft_allow_enabled=False вызова нет
    s.storage_dir = tmp_path
    fb = FakeBot(s)

    probe_called = False

    async def mock_probe(*args, **kwargs):
        nonlocal probe_called
        probe_called = True
        return []

    monkeypatch.setattr("avia.phone_check.probe_denied_pairs", mock_probe)

    # Запускаем _maintenance_loop на короткий промежуток
    loop_task = asyncio.create_task(fb.bot._maintenance_loop(tick_seconds=0.01))
    await asyncio.sleep(0.05)
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    assert not probe_called

    # 3. При включённой настройке probe_denied_pairs вызывается
    fb.bot.s.soft_allow_enabled = True
    loop_task = asyncio.create_task(fb.bot._maintenance_loop(tick_seconds=0.01))
    await asyncio.sleep(0.05)
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    assert probe_called


# Дополнительно: блокировка одновременного запуска (asyncio.Lock)
@pytest.mark.asyncio
async def test_soft_allow_concurrency_lock(tmp_path, monkeypatch):
    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_test")
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Redmi 1", status="active", sites_allow=[])
    reg.update_snapshot("dev1", online=True)

    settings = Settings(sites=[s1], storage_dir=tmp_path, phones=reg)

    started_ev = asyncio.Event()
    block_ev = asyncio.Event()

    async def mock_snapshot(s, device_id, url, **kwargs):
        started_ev.set()
        await block_ev.wait()
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    task1 = asyncio.create_task(probe_denied_pairs(settings))
    await started_ev.wait()

    # Второй запуск во время работы первого немедленно возвращает пустой список
    task2 = asyncio.create_task(probe_denied_pairs(settings))
    res2 = await task2
    assert res2 == []

    block_ev.set()
    res1 = await task1
    assert len(res1) == 1


# Дополнительно: сверка цены с эталоном (отклонение > 15% не одобряется)
@pytest.mark.asyncio
async def test_soft_allow_rejects_price_divergence(tmp_path, monkeypatch):
    # Эталон 5000, парсер возвращает 6000 (разница 20% > 15%)
    def parser_expensive(snap, q):
        return [Offer(site="site1", price=6000)]

    parser_expensive.results_complete = lambda snap: True
    PARSERS["p_exp"] = parser_expensive

    s1 = SiteConfig(name="site1", url_template="https://s1", phone="auto", parser="p_exp")
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Redmi 1", status="active", sites_allow=[])
    reg.update_snapshot("dev1", online=True)

    settings = Settings(sites=[s1], storage_dir=tmp_path, phones=reg)

    # Подменяем get_reference_price
    monkeypatch.setattr("avia.phone_check.get_reference_price", lambda *a, **kw: 5000.0)

    async def mock_snapshot(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    res = await probe_denied_pairs(settings)
    assert len(res) == 1
    assert res[0] == ("site1", "dev1", False)
    assert "site1" not in reg.get("dev1")["sites_allow"]
