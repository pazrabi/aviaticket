"""Тесты маршрутизации телефонов по скорости (speed routing), карантина и защиты батареи."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import sqlite3
import time

import pytest

from avia.admin_panel import build_parse, build_phone_order
from avia.config import Settings, SiteConfig
from avia.droidhub import PreferenceList, candidates
from avia.metrics import MetricsStore
from avia.models import Query
from avia.phones import PhoneRegistry
from avia.speed import SpeedTable, get_speed_table, reset_speed_tables


@pytest.fixture(autouse=True)
def clean_speed_caches():
    reset_speed_tables()
    yield
    reset_speed_tables()


# ---------------------------------------------------------------------------
# 1. Миграция priority на старой базе данных
# ---------------------------------------------------------------------------


def test_migration_priority_column_old_db(tmp_path):
    """Миграция: добавление priority в старую БД, чтение старых строк с NULL как live (0)."""
    db_path = tmp_path / "old_metrics.sqlite"
    # Создаём старую схему без колонки priority
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE site_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL NOT NULL,
                source        TEXT NOT NULL,
                site          TEXT NOT NULL,
                ok            INTEGER NOT NULL,
                partial       INTEGER NOT NULL,
                captcha       INTEGER NOT NULL,
                error         TEXT,
                offers        INTEGER NOT NULL,
                elapsed       REAL NOT NULL,
                device        TEXT,
                queue_wait    REAL,
                fetch_seconds REAL
            )
            """
        )
        # Старая запись (priority = NULL)
        conn.execute(
            """
            INSERT INTO site_runs (
                ts, source, site, ok, partial, captcha, error, offers, elapsed,
                device, queue_wait, fetch_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1000.0, "user", "aviasales", 1, 0, 0, None, 5, 2.5, "phone1", 0.1, 2.5),
        )

    # Инициализация MetricsStore должна мигрировать таблицу
    store = MetricsStore(db_path)
    store.record_run(
        source="bg",
        site="aviasales",
        ok=True,
        partial=False,
        captcha=False,
        error=None,
        offers=3,
        elapsed=4.0,
        ts=1010.0,
        device="phone1",
        queue_wait=0.2,
        fetch_seconds=4.0,
        priority=2,
    )

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(site_runs)").fetchall()}
        assert "priority" in cols

    table = SpeedTable(metrics_path=db_path)
    table.refresh(now=1020.0)

    # Старая строка с NULL priority считается как live (0)
    live_st = table.get_stats("aviasales", "phone1", "live", now=1020.0)
    assert live_st.n == 1
    assert live_st.median_fetch == 2.5

    # Новая строка с priority=2 попадает в bg
    bg_st = table.get_stats("aviasales", "phone1", "bg", now=1020.0)
    assert bg_st.n == 1
    assert bg_st.median_fetch == 4.0


# ---------------------------------------------------------------------------
# 2. Порядок кандидатов для людей (live) и фона (bg) по замерам скорости
# ---------------------------------------------------------------------------


def test_order_live_and_bg_by_measurements(tmp_path):
    """priority 0 сортирует быстрые первыми; priority > 0 сортирует медленные первыми."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # pixel: быстрый (медиана 4.0с, n=6)
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=4.0, ts=t0 + i, device="pixel", fetch_seconds=4.0, priority=0)
    # redmi: средний (медиана 8.0с, n=6)
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=8.0, ts=t0 + i, device="redmi", fetch_seconds=8.0, priority=0)
    # honor: медленный (медиана 10.0с, n=6)
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=10.0, ts=t0 + i, device="honor", fetch_seconds=10.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="pixel,redmi,honor")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)

    # Для людей (priority=0): pixel (4.0) -> redmi (8.0) -> honor (10.0)
    live_cands = candidates(settings, site, priority=0)
    assert list(live_cands) == ["pixel", "redmi", "honor"]

    # Для фона (priority=2): honor (10.0) -> redmi (8.0) -> pixel (4.0)
    bg_cands = candidates(settings, site, priority=2)
    assert list(bg_cands) == ["honor", "redmi", "pixel"]
    # Для фона быстрый (pixel) отделён bg_patience_seconds
    assert bg_cands.patience == 60.0
    assert bg_cands.preferred_count == 2  # honor и redmi медленнее pixel


# ---------------------------------------------------------------------------
# 3. Выпадение пары с ok_rate < 0.4
# ---------------------------------------------------------------------------


def test_pair_dropped_on_low_ok_rate(tmp_path):
    """Пара с ok_rate < 0.4 при n >= min_samples выпадает в самый конец."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # phone_good: n=10, 10 ok (ok_rate = 1.0, 5.0с)
    for i in range(10):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=5.0, ts=t0 + i, device="phone_good", fetch_seconds=5.0, priority=0)

    # phone_bad: n=10, 2 ok, 8 fail (ok_rate = 0.2 < 0.4, 4.0с)
    for i in range(2):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=4.0, ts=t0 + i, device="phone_bad", fetch_seconds=4.0, priority=0)
    for i in range(2, 10):
        store.record_run("user", "as", ok=False, partial=False, captcha=False, error="502",
                         offers=0, elapsed=1.0, ts=t0 + i, device="phone_bad", fetch_seconds=1.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="phone_bad,phone_good")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)

    # phone_bad изначально первый в site.phone и быстрее, но из-за ok_rate=0.2 уходит в конец
    cands_live = candidates(settings, site, priority=0)
    assert list(cands_live) == ["phone_good", "phone_bad"]

    # Для фона непригодные тоже уходят в конец
    cands_bg = candidates(settings, site, priority=1)
    assert list(cands_bg) == ["phone_good", "phone_bad"]


# ---------------------------------------------------------------------------
# 4. Карантин после 3 неудач и пробная попытка только от фона
# ---------------------------------------------------------------------------


def test_quarantine_after_3_failures_and_bg_trial(tmp_path):
    """3 неудачи подряд -> карантин на 10 минут. После 10 мин пробная попытка только от фона."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t_fail = 1000.0

    # 3 неудачи подряд на dev_flaky
    store.record_run("user", "as", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=1.0, ts=t_fail, device="dev_flaky", priority=0)
    store.record_run("user", "as", ok=False, partial=False, captcha=True, error="капча",
                     offers=0, elapsed=1.0, ts=t_fail + 10, device="dev_flaky", priority=0)
    store.record_run("user", "as", ok=True, partial=True, captcha=False, error="неполная выдача",
                     offers=0, elapsed=1.0, ts=t_fail + 20, device="dev_flaky", priority=0)

    # dev_stable работает стабильно
    for i in range(5):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=5.0, ts=t_fail + i, device="dev_stable", fetch_seconds=5.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="dev_flaky,dev_stable")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, speed_quarantine_seconds=600)
    speed_table = get_speed_table(settings)

    # 1. Спустя 2 минуты (t=1140 < 1000 + 20 + 600 = 1620) — активный карантин
    speed_table.refresh(now=1140.0)
    pst = speed_table.get_pair_status("as", "dev_flaky", now=1140.0)
    assert pst.in_quarantine is True
    assert pst.awaiting_trial is False

    # Люди получают только dev_stable
    cands_live = speed_table.order_candidates(site, ["dev_flaky", "dev_stable"], priority=0, now=1140.0)
    assert list(cands_live) == ["dev_stable", "dev_flaky"]

    # Фон тоже не берет dev_flaky во время активного карантина
    cands_bg = speed_table.order_candidates(site, ["dev_flaky", "dev_stable"], priority=2, now=1140.0)
    assert list(cands_bg) == ["dev_stable", "dev_flaky"]

    # 2. Спустя 12 минут (t=1740 > 1620) — карантин истёк, пара ждёт пробной попытки
    speed_table.refresh(now=1740.0)
    pst_after = speed_table.get_pair_status("as", "dev_flaky", now=1740.0)
    assert pst_after.in_quarantine is False
    assert pst_after.awaiting_trial is True

    # Люди НЕ берут пробную попытку (люди пробным не становятся)
    cands_live_after = speed_table.order_candidates(site, ["dev_flaky", "dev_stable"], priority=0, now=1740.0)
    assert list(cands_live_after) == ["dev_stable", "dev_flaky"]

    # Фон направляется на пробную попытку ПЕРВЫМ
    cands_bg_after = speed_table.order_candidates(site, ["dev_flaky", "dev_stable"], priority=2, now=1740.0)
    assert list(cands_bg_after) == ["dev_flaky", "dev_stable"]

    # 3. Успешная пробная попытка фоном снимает карантин
    store.record_run("bg", "as", ok=True, partial=False, captcha=False, error=None,
                     offers=10, elapsed=4.0, ts=1750.0, device="dev_flaky", fetch_seconds=4.0, priority=2)
    speed_table.refresh(now=1760.0)
    pst_recovered = speed_table.get_pair_status("as", "dev_flaky", now=1760.0)
    assert pst_recovered.consecutive_failures == 0
    assert pst_recovered.in_quarantine is False
    assert pst_recovered.awaiting_trial is False


# ---------------------------------------------------------------------------
# 5. Новые пары исследуются фоном (n == 0)
# ---------------------------------------------------------------------------


def test_new_pairs_explored_first_by_bg(tmp_path):
    """Новые пары (n==0) фоновые задачи направляют первыми; люди новые пары первыми не берут."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # dev_measured: есть замеры
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=5.0, ts=t0 + i, device="dev_measured", fetch_seconds=5.0, priority=0)

    # dev_new: замеров нет (n == 0)
    site = SiteConfig(name="as", url_template="https://test", phone="dev_measured,dev_new")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)

    # Для людей: проверенный телефон первый, новый — запасной в конце
    cands_live = candidates(settings, site, priority=0)
    assert list(cands_live) == ["dev_measured", "dev_new"]

    # Для фона: новый телефон первый для набора замеров без вреда людям
    cands_bg = candidates(settings, site, priority=1)
    assert list(cands_bg) == ["dev_new", "dev_measured"]


# ---------------------------------------------------------------------------
# 6. slow_ratio и расчёт patience от разницы медиан
# ---------------------------------------------------------------------------


def test_slow_ratio_and_patience_calculation(tmp_path):
    """Телефоны медленнее лучшего в slow_ratio (3) раз идут в конец с терпением от разницы медиан."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # pixel: лучший, медиана 4.0с
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=4.0, ts=t0 + i, device="pixel", fetch_seconds=4.0, priority=0)

    # redmi: медиана 6.0с (6.0 <= 4.0 * 3.0 = 12.0) — основной быстрый
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=6.0, ts=t0 + i, device="redmi", fetch_seconds=6.0, priority=0)

    # honor: медиана 16.0с (16.0 > 12.0) — медленный запасной
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=16.0, ts=t0 + i, device="honor", fetch_seconds=16.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="honor,pixel,redmi", phone_patience_seconds=0)
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, speed_slow_ratio=3.0)

    cands = candidates(settings, site, priority=0)
    # Порядок строится по возрастанию C: pixel (4.0) -> redmi (6.0) -> honor (16.0)
    assert list(cands) == ["pixel", "redmi", "honor"]
    assert isinstance(cands, PreferenceList)
    # Шаг 4: правило «медленнее в 3 раза» заменено на фильтр p_lower >= speed_live_min_p_lower.
    # Так как у honor 6 из 6 успешных запусков (p_lower >= 0.6), он допущен в основные,
    # все 3 телефона являются основными (preferred_count=3), а patience=0.0 (запасных нет).
    assert cands.preferred_count == 3
    assert cands.patience == 0.0


# ---------------------------------------------------------------------------
# 7. Защита по батарее (< phone_min_battery и не на зарядке)
# ---------------------------------------------------------------------------


def test_battery_protection_and_charging(tmp_path):
    """Батарея < phone_min_battery (15%) не на зарядке исключает телефон; на зарядке телефон работает."""
    reg = PhoneRegistry(tmp_path / "phones.json", phone_min_battery=15)
    reg.register("dev_low", name="Dev Low", status="active")
    reg.register("dev_charging", name="Dev Charging", status="active")
    reg.register("dev_ok", name="Dev OK", status="active")

    # dev_low: 10% батареи, не на зарядке
    reg.update_snapshot("dev_low", online=True, battery=10, charging=False)
    # dev_charging: 10% батареи, на зарядке
    reg.update_snapshot("dev_charging", online=True, battery=10, charging=True)
    # dev_ok: 50% батареи, не на зарядке
    reg.update_snapshot("dev_ok", online=True, battery=50, charging=False)

    site = SiteConfig(name="as", url_template="https://test", phone="dev_low,dev_charging,dev_ok")
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg, phone_min_battery=15)

    cands = candidates(settings, site)
    # dev_low исключён из кандидатов
    assert "dev_low" not in cands
    assert list(cands) == ["dev_charging", "dev_ok"]

    # При phone_min_battery=0 защита отключена
    settings.phone_min_battery = 0
    reg.phone_min_battery = 0
    cands_disabled = candidates(settings, site)
    assert "dev_low" in cands_disabled


# ---------------------------------------------------------------------------
# 8. SPEED_ROUTING=0 даёт прежний порядок
# ---------------------------------------------------------------------------


def test_speed_routing_disabled_legacy_behavior(tmp_path, monkeypatch):
    """При SPEED_ROUTING=0 поведение в точности прежнее (сохраняется порядок site.phone, батарея не фильтрует)."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # pixel быстрее redmi
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=2.0, ts=t0 + i, device="pixel", fetch_seconds=2.0, priority=0)
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=10.0, ts=t0 + i, device="redmi", fetch_seconds=10.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="redmi,pixel", phone_patience_seconds=5)
    # speed_routing=False
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, speed_routing=False)

    # При speed_routing=False порядок строго из site.phone (redmi, pixel)
    cands_live = candidates(settings, site, priority=0)
    assert list(cands_live) == ["redmi", "pixel"]
    assert cands_live.patience == 5.0

    cands_bg = candidates(settings, site, priority=1)
    assert list(cands_bg) == ["redmi", "pixel"]


# ---------------------------------------------------------------------------
# 9. Блок «Порядок телефонов» в /admin -> Парсинг
# ---------------------------------------------------------------------------


def test_admin_panel_phone_order_block(tmp_path):
    """В /admin -> Парсинг отображается блок 'Порядок телефонов' с медианой, ok% и n."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    for i in range(5):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=3.5, ts=t0 + i, device="pixel", fetch_seconds=3.5, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="pixel")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)

    ctx = {
        "settings": settings,
        "summary": store.summary(0.0),
        "devices_summary": store.summary_by_device(0.0),
        "site_devices_summary": store.summary_by_site_device(0.0),
        "counts_by_source": {},
        "recent_errors": [],
    }

    text = build_phone_order(ctx)
    assert "Порядок телефонов:" in text
    assert "as:" in text
    assert "люди: pixel (3.5 с · 100% · n=5)" in text
    assert "фон: pixel" in text


# ---------------------------------------------------------------------------
# 10. Отказ -> следующий самый быстрый: повтор и запись провала с device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_retry_on_failure_and_metric_recorded(tmp_path, monkeypatch):
    """При 502/капче/неполной выдаче без офферов запрос повторяется на следующем телефоне, провал пишется с device."""
    from avia.collector import Snapshot
    from avia.models import Offer, Query
    from avia.parsers import PARSERS
    from avia.pipeline import _from_browser

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig(
        name="test_site",
        url_template="https://test.site/{origin}{destination}",
        phone="dev_fail,dev_ok",
        parser="test_parser",
    )
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )

    def dummy_parser(snap, q):
        if "EMPTY" in snap.text:
            return []
        return [Offer(site="test_site", price=5500)]

    monkeypatch.setitem(PARSERS, "test_parser", dummy_parser)

    # 1. Тест: dev_fail возвращает капчу -> попытка повторяется на dev_ok
    call_log = []

    async def fake_fetch_any(s, candidates, url, **kwargs):
        st = kwargs.get("stats")
        dev = candidates[0]
        call_log.append(dev)
        if st is not None:
            st["device"] = dev
            st["queue_wait"] = 0.1
            st["fetch_seconds"] = 1.0

        if dev == "dev_fail":
            return Snapshot(url=url, text="Капча", blocked=True), True
        return Snapshot(url=url, text="Успех", blocked=False), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)

    q = Query("MOW", "LED", date(2026, 12, 10))
    offers, page_url, partial = await _from_browser(site, q, settings, priority=0)

    # Запрос повторён на следующем кандидате
    assert call_log == ["dev_fail", "dev_ok"]
    assert len(offers) == 1
    assert offers[0].price == 5500

    # Провал первого кандидата записан в метрики с device="dev_fail"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT device, ok, captcha FROM site_runs WHERE device='dev_fail'").fetchall()
        assert len(rows) >= 1
        assert rows[0]["device"] == "dev_fail"
        assert rows[0]["captcha"] == 1
        assert rows[0]["ok"] == 0


@pytest.mark.asyncio
async def test_from_browser_retries_on_parser_empty_error_and_records_metrics(tmp_path, monkeypatch):
    """При неполной выдаче без офферов ParserEmptyError перехватывается, провал пишется в метрики и опрашивается следующий телефон."""
    from avia.collector import Snapshot
    from avia.models import Offer, Query
    from avia.parsers import PARSERS
    from avia.pipeline import _from_browser

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig(
        name="empty_test_site",
        url_template="https://test.site/{origin}{destination}",
        phone="dev1,dev2",
        parser="empty_test_parser",
    )
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )

    def empty_parser(snap, q):
        if "EMPTY" in snap.text:
            return []
        return [Offer(site="empty_test_site", price=4200)]

    monkeypatch.setitem(PARSERS, "empty_test_parser", empty_parser)

    call_log = []

    async def fake_fetch_any(s, cands, url, **kwargs):
        st = kwargs.get("stats")
        dev = cands[0]
        call_log.append(dev)
        if st is not None:
            st["device"] = dev
            st["queue_wait"] = 0.2
            st["fetch_seconds"] = 1.5
            st["tried"] = [dev]
        if dev == "dev1":
            # Текст > 200 символов без офферов -> _process_snapshot бросит ParserEmptyError
            long_text = "EMPTY " * 60
            return Snapshot(url=url, text=long_text, blocked=False), True
        return Snapshot(url=url, text="Билеты найдены", blocked=False), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)

    q = Query("MOW", "LED", date(2026, 12, 10))
    offers, page_url, partial = await _from_browser(site, q, settings, priority=0)

    # dev1 упал с ParserEmptyError -> запрос переключился на dev2
    assert call_log == ["dev1", "dev2"]
    assert len(offers) == 1
    assert offers[0].price == 4200

    # Провал dev1 зафиксирован в site_runs
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT device, ok, partial, error FROM site_runs WHERE device='dev1'").fetchall()
        assert len(rows) == 1
        assert rows[0]["device"] == "dev1"
        assert rows[0]["ok"] == 0
        assert rows[0]["partial"] == 1
        assert "неполная выдача без офферов" in rows[0]["error"]


def test_speed_table_bg_preserves_unmeasured_eligible_candidates(tmp_path):
    """В priority > 0 кандидат с est is None не отбрасывается и входит в slow_list перед быстрыми."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # pixel: быстрый (медиана 4.0с, n=6)
    for i in range(6):
        store.record_run("bg", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=4.0, ts=t0 + i, device="pixel", fetch_seconds=4.0, priority=2)

    # redmi: медленный (медиана 12.0с, n=6)
    for i in range(6):
        store.record_run("bg", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=12.0, ts=t0 + i, device="redmi", fetch_seconds=12.0, priority=2)

    # unmeasured: 1 запуск ok, но fetch_seconds=None (st.n=1, est is None)
    store.record_run("bg", "as", ok=True, partial=False, captcha=False, error=None,
                     offers=5, elapsed=0.0, ts=t0 + 20, device="unmeasured", fetch_seconds=None, priority=2)

    site = SiteConfig(name="as", url_template="https://test", phone="pixel,redmi,unmeasured")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    # Принудительно очистим медиану сайта чтобы est остался None
    table._site_medians.clear()

    ordered = table.order_candidates(site, ["pixel", "redmi", "unmeasured"], priority=2, now=t0 + 50)
    # unmeasured НЕ должен исчезнуть!
    assert "unmeasured" in ordered
    assert "redmi" in ordered
    assert "pixel" in ordered
    # Медленный redmi и unmeasured идут перед быстрым pixel
    assert list(ordered) == ["redmi", "unmeasured", "pixel"]


@pytest.mark.asyncio
async def test_from_browser_passes_site_name_to_fetch_page_snapshot_any(tmp_path, monkeypatch):
    """_from_browser передает site_name=site.name в fetch_page_snapshot_any, и сбой пишется с правильным сайтом."""
    from avia.collector import Snapshot
    from avia.models import Offer, Query
    from avia.parsers import PARSERS
    from avia.pipeline import _from_browser

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig(
        name="custom_mirror_site",
        url_template="https://mirror.unknown-domain.org/{origin}{destination}",
        phone="dev_fail,dev_ok",
        parser="custom_parser",
    )
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )

    passed_kwargs = {}

    async def fake_fetch_any(s, cands, url, **kwargs):
        passed_kwargs.update(kwargs)
        dev = cands[0]
        st = kwargs.get("stats")
        if st is not None:
            st["device"] = dev
            st["queue_wait"] = 0.1
            st["fetch_seconds"] = 1.0
            st["tried"] = [dev]
        if dev == "dev_fail":
            # Имитируем сбой с записью через droidhub._record_failure
            from avia.droidhub import _record_failure
            _record_failure(
                settings=s,
                site=kwargs.get("site_name") or "site",
                device=dev,
                priority=kwargs.get("priority", 0),
                error="502 Bad Gateway",
            )
            return Snapshot(url=url, text="Капча", blocked=True), True
        return Snapshot(url=url, text="Успех", blocked=False), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)
    monkeypatch.setitem(PARSERS, "custom_parser", lambda snap, q: [Offer(site="custom_mirror_site", price=3000)])

    q = Query("MOW", "LED", date(2026, 12, 10))
    offers, page_url, partial = await _from_browser(site, q, settings, priority=0)

    assert passed_kwargs.get("site_name") == "custom_mirror_site"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT site, device, error FROM site_runs WHERE device='dev_fail'").fetchall()
        assert len(rows) >= 1
        assert rows[0]["site"] == "custom_mirror_site"
        assert rows[0]["site"] != "site"


@pytest.mark.asyncio
async def test_from_browser_removes_all_tried_devices(tmp_path, monkeypatch):
    """Все опробованные устройства (tried) удаляются из remaining_cands, предотвращая повторный опрос упавшего телефона."""
    from avia.collector import Snapshot
    from avia.models import Offer, Query
    from avia.parsers import PARSERS
    from avia.pipeline import _from_browser

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig(
        name="test_site",
        url_template="https://test.site/{origin}{destination}",
        phone="dev1,dev2,dev3",
        parser="test_p",
    )
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="secret",
    )

    received_cands_history = []

    async def fake_fetch_any(s, cands, url, **kwargs):
        received_cands_history.append(list(cands))
        st = kwargs.get("stats")
        # При первом вызове fake_fetch_any пробует dev1 (упал внутри) и dev2 (вернул капчу)
        if "dev1" in cands:
            if st is not None:
                st["device"] = "dev2"
                st["tried"] = ["dev1", "dev2"]
                st["queue_wait"] = 0.5
                st["fetch_seconds"] = 1.0
            return Snapshot(url=url, text="Капча", blocked=True), True
        else:
            # Второй вызов должен содержать ТОЛЬКО dev3!
            if st is not None:
                st["device"] = "dev3"
                st["tried"] = ["dev3"]
                st["queue_wait"] = 0.1
                st["fetch_seconds"] = 1.0
            return Snapshot(url=url, text="Успех", blocked=False), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)
    monkeypatch.setitem(PARSERS, "test_p", lambda snap, q: [Offer(site="test_site", price=2500)])

    q = Query("MOW", "LED", date(2026, 12, 10))
    offers, page_url, partial = await _from_browser(site, q, settings, priority=0)

    # 1-й вызов: dev1, dev2, dev3.
    # dev1 и dev2 были опробованы.
    # 2-й вызов должен получить ТОЛЬКО dev3 (dev1 не должен повторяться!)
    assert len(received_cands_history) == 2
    assert received_cands_history[0] == ["dev1", "dev2", "dev3"]
    assert received_cands_history[1] == ["dev3"]


def test_phone_auto_speed_routing_preference_list(tmp_path):
    """Сайт с phone='auto' при наличии замеров возвращает PreferenceList с правильной сортировкой."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # auto_fast: 3.0с
    for i in range(6):
        store.record_run("user", "site_auto", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=3.0, ts=t0 + i, device="auto_fast", fetch_seconds=3.0, priority=0)

    # auto_slow: 9.0с
    for i in range(6):
        store.record_run("user", "site_auto", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=9.0, ts=t0 + i, device="auto_slow", fetch_seconds=9.0, priority=0)

    site = SiteConfig("site_auto", "https://auto.test", phone="auto")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="secret",
        droidhub_devices=["auto_slow", "auto_fast"],
    )

    # Для людей быстрый телефон идет первым и возвращается PreferenceList
    cands_live = candidates(settings, site, priority=0)
    assert isinstance(cands_live, PreferenceList)
    assert list(cands_live) == ["auto_fast", "auto_slow"]
    assert cands_live.prefer_order is True

    # Для фона медленный телефон идет первым
    cands_bg = candidates(settings, site, priority=2)
    assert isinstance(cands_bg, PreferenceList)
    assert list(cands_bg) == ["auto_slow", "auto_fast"]
    assert cands_bg.prefer_order is True


@pytest.mark.asyncio
async def test_fetch_page_snapshot_any_resets_patience_on_fallbacks(monkeypatch, tmp_path):
    """Когда предпочтительные телефоны исчерпаны, терпение сбрасывается в 0, а preferred_count равен числу оставшихся."""
    from avia.droidhub import fetch_page_snapshot_any, PreferenceList, DroidhubError
    from avia.collector import Snapshot

    acquired_calls = []

    async def fake_acquire(cands, **kwargs):
        acquired_calls.append((list(cands), kwargs))
        return cands[0]

    async def fake_release(dev):
        pass

    attempt = 0
    async def fake_fetch_device(settings, dev, url, **kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise DroidhubError("502 Bad Gateway")
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.droidhub.acquire", fake_acquire)
    monkeypatch.setattr("avia.droidhub.release", fake_release)
    monkeypatch.setattr("avia.droidhub._fetch_device_snapshot", fake_fetch_device)

    settings = Settings(sites=[], storage_dir=tmp_path, droidhub_url="https://hub.test", droidhub_token="t")
    cand_pref = PreferenceList(["p1", "s1", "s2"], patience=30.0, preferred_count=1)

    snap, fin = await fetch_page_snapshot_any(settings, cand_pref, "https://test.site")
    assert snap.text == "OK"
    assert len(acquired_calls) == 2
    # 1-й вызов: предпочтительный p1, patience=30.0, preferred_count=1
    assert acquired_calls[0][0] == ["p1", "s1", "s2"]
    assert acquired_calls[0][1]["patience"] == 30.0
    assert acquired_calls[0][1]["preferred_count"] == 1
    # 2-й вызов: предпочтительных не осталось, patience=0.0, preferred_count=2
    assert acquired_calls[1][0] == ["s1", "s2"]
    assert acquired_calls[1][1]["patience"] == 0.0
    assert acquired_calls[1][1]["preferred_count"] == 2


@pytest.mark.asyncio
async def test_no_duplicate_failure_record_on_last_candidate(tmp_path, monkeypatch):
    """При сбое последнего/единственного кандидата запись в site_runs добавляется ровно один раз через SiteResult."""
    from avia.pipeline import _run_site
    from avia.models import Query
    from avia.droidhub import DroidhubError
    from avia.parsers import PARSERS

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)

    site = SiteConfig(name="single_fail", url_template="https://test", phone="only_one", parser="p_test")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        metrics=store,
        droidhub_url="https://hub.test",
        droidhub_token="t",
    )

    monkeypatch.setitem(PARSERS, "p_test", lambda snap, q: [])

    async def fake_fetch_device(settings, dev, url, **kwargs):
        raise DroidhubError("502 Bad Gateway")

    monkeypatch.setattr("avia.droidhub._fetch_device_snapshot", fake_fetch_device)

    q = Query("MOW", "LED", date(2026, 12, 10))
    res = await _run_site(site, q, settings, priority=0)
    assert not res.ok

    store.record_results("user", [res])

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT site, device, ok, error FROM site_runs WHERE device='only_one'").fetchall()
        assert len(rows) == 1
        assert rows[0]["ok"] == 0


def test_admin_panel_shows_hold_and_low_battery_badges(tmp_path):
    """В блоке 'Порядок телефонов' отображаются бейджи [заморозка] и [пониженная батарея] для отсеянных телефонов."""
    from avia.admin_panel import build_phone_order
    from avia.phones import PhoneRegistry

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0
    store.record_run("user", "test_site", ok=True, partial=False, captcha=False, error=None,
                     offers=5, elapsed=3.0, ts=t0, device="p_ok", fetch_seconds=3.0, priority=0)

    reg = PhoneRegistry(tmp_path / "phones.json", phone_min_battery=20)
    reg.register("p_ok", name="Pixel OK", status="active")
    reg.register("p_hold", name="Pixel Hold", status="hold")
    reg.register("p_low_bat", name="Pixel Low Bat", status="active")

    reg.update_snapshot("p_ok", online=True, battery=80, charging=False)
    reg.update_snapshot("p_hold", online=True, battery=90, charging=False)
    reg.update_snapshot("p_low_bat", online=True, battery=10, charging=False)

    site = SiteConfig(name="test_site", url_template="https://test", phone="p_ok,p_hold,p_low_bat")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, phones=reg, phone_min_battery=20)

    ctx = {
        "settings": settings,
        "summary": store.summary(0.0),
        "devices_summary": store.summary_by_device(0.0),
        "site_devices_summary": store.summary_by_site_device(0.0),
        "counts_by_source": {},
        "recent_errors": [],
    }

    text = build_phone_order(ctx)
    assert "Порядок телефонов:" in text
    assert "test_site:" in text
    assert "p_hold" in text
    assert "[заморозка]" in text
    assert "p_low_bat" in text
    assert "[пониженная батарея]" in text


def test_speed_table_pair_total_counts_and_exploratory_fallback(tmp_path):
    """Телефон с замерами не определяется как exploratory в фоне, даже если не попал в последние 2000 записей."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    now = time.time()
    t_old = now - (3 * 86400)

    store.record_run("bg", "site1", ok=True, partial=False, captcha=False, error=None,
                     offers=3, elapsed=5.0, ts=t_old, device="p_known", fetch_seconds=5.0, priority=2)

    with sqlite3.connect(db_path) as conn:
        dummy_rows = [
            (t_old + 10 + i, "user", "other_site", 1, 0, 0, None, 1, 2.0, "other_dev", 0.1, 2.0, 0)
            for i in range(2050)
        ]
        conn.executemany(
            """INSERT INTO site_runs (ts, source, site, ok, partial, captcha, error, offers, elapsed, device, queue_wait, fetch_seconds, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            dummy_rows
        )

    site = SiteConfig(name="site1", url_template="https://test", phone="p_known,p_brand_new")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=now)

    pst = table.get_pair_status("site1", "p_known")
    assert pst.total_runs >= 1

    cands_bg = table.order_candidates(site, ["p_known", "p_brand_new"], priority=2, now=now)
    assert list(cands_bg) == ["p_brand_new", "p_known"]


def test_speed_table_closes_sqlite_connection(tmp_path, monkeypatch):
    """SpeedTable.refresh гарантированно закрывает соединение с SQLite."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    store.record_run("user", "s", ok=True, partial=False, captcha=False, error=None,
                     offers=1, elapsed=1.0, ts=time.time(), device="d", fetch_seconds=1.0, priority=0)

    closed = False
    orig_connect = sqlite3.connect

    class ConnWrapper:
        def __init__(self, real_conn):
            self._real = real_conn
        def __getattr__(self, item):
            return getattr(self._real, item)
        def __setattr__(self, name, value):
            if name == "_real":
                super().__setattr__(name, value)
            else:
                setattr(self._real, name, value)
        def __enter__(self):
            return self._real.__enter__()
        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._real.__exit__(exc_type, exc_val, exc_tb)
        def close(self):
            nonlocal closed
            closed = True
            return self._real.close()

    def fake_connect(*args, **kwargs):
        return ConnWrapper(orig_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", fake_connect)

    settings = Settings(sites=[], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh()

    assert closed is True
