"""Тесты ручной проверки новых и активных телефонов хаба (phone_check)."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import time
from typing import Any

import pytest

from avia.admin_panel import build_phones, make_phones_keyboard
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.droidhub import DroidhubError, candidates
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.phone_check import (
    PassResult,
    evaluate_site,
    get_reference_price,
    is_checking,
    run_phone_check,
    run_site_pass,
    set_checking,
)
from avia.phones import PhoneRegistry


class FakeBot:
    """Фейковый бот для тестов без сети."""

    def __init__(self, settings: Settings):
        from avia.bot import Bot

        if not settings.tg_token:
            settings.tg_token = "fake_tg_token"
        self.bot = Bot(settings)
        self.bot.call = self._mock_call  # type: ignore
        self.sent_messages: list[dict[str, Any]] = []

    async def _mock_call(self, method: str, **kwargs: Any) -> Any:
        if method in ("sendMessage", "editMessageText"):
            self.sent_messages.append(kwargs)
            return {"message_id": 100, **kwargs}
        if method == "answerCallbackQuery":
            self.sent_messages.append({"method": "answerCallbackQuery", **kwargs})
            return True
        return {}


# 1. Новый телефон со статусом new не попадает в candidates ни для людей, ни для фона
def test_new_phone_never_in_candidates(tmp_path):
    reg = PhoneRegistry(tmp_path / "phones.json", phones_new_policy="check")
    reg.register("active_phone", name="Active Phone", status="active", first_seen=10.0)
    reg.register("new_phone", name="New Phone", status="new", first_seen=20.0)

    # 1a. Одиночный телефон: если указан new_phone, возвращается []
    site_single = SiteConfig(name="s_single", url_template="https://test", phone="new_phone")
    settings = Settings(sites=[site_single], storage_dir=tmp_path, phones=reg)
    assert candidates(settings, site_single, priority=0) == []
    assert candidates(settings, site_single, priority=1) == []

    # 1b. Список через запятую: new_phone отфильтровывается
    site_list = SiteConfig(name="s_list", url_template="https://test", phone="active_phone,new_phone")
    settings_list = Settings(sites=[site_list], storage_dir=tmp_path, phones=reg)
    cands_p0 = list(candidates(settings_list, site_list, priority=0))
    cands_p1 = list(candidates(settings_list, site_list, priority=1))
    assert "new_phone" not in cands_p0
    assert "new_phone" not in cands_p1
    assert "active_phone" in cands_p0
    assert "active_phone" in cands_p1

    # 1c. auto: new_phone не попадает в candidates
    site_auto = SiteConfig(name="s_auto", url_template="https://test", phone="auto")
    settings_auto = Settings(
        sites=[site_auto],
        storage_dir=tmp_path,
        phones=reg,
        droidhub_devices=["active_phone", "new_phone"],
    )
    cands_auto_p0 = list(candidates(settings_auto, site_auto, priority=0))
    cands_auto_p1 = list(candidates(settings_auto, site_auto, priority=1))
    assert "new_phone" not in cands_auto_p0
    assert "new_phone" not in cands_auto_p1
    assert "active_phone" in cands_auto_p0


# 2. Сообщение админам уходит ровно один раз при обнаружении телефона
@pytest.mark.asyncio
async def test_notify_admin_once_on_hub_discovery(tmp_path, monkeypatch):
    settings = Settings(
        sites=[SiteConfig(name="as", url_template="https://test", phone="auto")],
        droidhub_url="https://hub.example.com",
        droidhub_token="token",
        tg_token="fake_tg",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
        phones_new_policy="check",
    )
    fb = FakeBot(settings)

    # 1-й опрос: первое обнаружение при пустом реестре регистрирует молча
    monkeypatch.setattr(
        fb.bot,
        "_fetch_hub_devices",
        lambda: asyncio.sleep(0, result=[{"id": "p1", "name": "Phone One", "battery": 90}]),
    )
    await fb.bot._check_hub_devices_once()
    assert len(fb.sent_messages) == 0

    # 2-й опрос: появляется p2 — админу уходит ровно 1 сообщение с предложением проверить
    monkeypatch.setattr(
        fb.bot,
        "_fetch_hub_devices",
        lambda: asyncio.sleep(
            0,
            result=[
                {"id": "p1", "name": "Phone One", "battery": 90},
                {"id": "p2", "name": "Phone Two", "battery": 82},
            ],
        ),
    )
    await fb.bot._check_hub_devices_once()
    assert len(fb.sent_messages) == 1
    msg_text = fb.sent_messages[0]["text"]
    assert "обнаружен телефон" in msg_text.lower()
    assert "Phone Two" in msg_text
    assert "p2" in msg_text
    assert "82%" in msg_text
    assert "🔍 Проверить" in msg_text

    # 3-й опрос с теми же устройствами: повторного оповещения НЕТ
    await fb.bot._check_hub_devices_once()
    assert len(fb.sent_messages) == 1


# 3. Кнопка проверки в админ-панели вместо заморозки/включения
def test_admin_panel_buttons_for_new_and_existing():
    phones = [
        {"id": "p_new", "name": "Redmi New", "status": "new"},
        {"id": "p_act", "name": "Redmi Act", "status": "active"},
        {"id": "p_hld", "name": "Honor Hold", "status": "hold"},
    ]
    kb = make_phones_keyboard(phones)
    rows = kb["inline_keyboard"]

    # Для p_new: только одна кнопка «🔍 Проверить Redmi New» (callback: adm:ph:test:p_new)
    new_row = rows[0]
    assert len(new_row) == 1
    assert "🔍 Проверить Redmi New" in new_row[0]["text"]
    assert new_row[0]["callback_data"] == "adm:ph:test:p_new"

    # Для p_act: заморозка с именем + узкая кнопка «🔄» перепроверки
    act_row = rows[1]
    assert len(act_row) == 2
    assert act_row[1] == {"text": "🔄", "callback_data": "adm:ph:re:p_act"}
    assert "❄️ Redmi Act" in act_row[0]["text"]
    assert act_row[0]["callback_data"] == "adm:ph:hold:p_act"

    # Для p_hld: кнопка включения + «🔄»
    hld_row = rows[2]
    assert len(hld_row) == 2
    assert hld_row[1]["callback_data"] == "adm:ph:re:p_hld"
    assert "▶️ Honor Hold" in hld_row[0]["text"]
    assert hld_row[0]["callback_data"] == "adm:ph:on:p_hld"


# 4. Неадмин не может запустить проверку ни по кнопке, ни по команде
@pytest.mark.asyncio
async def test_non_admin_cannot_trigger_check(tmp_path, monkeypatch):
    settings = Settings(
        sites=[SiteConfig(name="as", url_template="https://test", phone="auto")],
        tg_token="fake",
        tg_allowed_ids={100},
        storage_dir=tmp_path,
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev1", name="Dev One", status="new")

    called = False

    async def mock_run_check(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("avia.phone_check.run_phone_check", mock_run_check)

    # 4a. Не-администратор шлёт команду /phone dev1 test
    msg_stranger = {
        "chat": {"id": 999, "type": "private"},
        "from": {"id": 999},
        "text": "/phone dev1 test",
    }
    await fb.bot.handle(msg_stranger)
    assert not called
    assert len(fb.sent_messages) == 0

    # 4b. Не-администратор жмёт inline-кнопку adm:ph:test:dev1
    cq_stranger = {
        "id": "cq999",
        "from": {"id": 999},
        "message": {"chat": {"id": 999}, "message_id": 50},
        "data": "adm:ph:test:dev1",
    }
    await fb.bot.handle_callback(cq_stranger)
    assert not called
    assert any("Нет доступа" in str(m) for m in fb.sent_messages)


# 5. Успешный допуск по «ок» и выставление sites_allow
@pytest.mark.asyncio
async def test_phone_check_admit_success_sets_active_and_sites_allow(tmp_path, monkeypatch):
    site1 = SiteConfig(name="site_ok", url_template="https://ok", phone="auto", parser="mock_ok")
    site2 = SiteConfig(name="site_fail", url_template="https://fail", phone="auto", parser="mock_fail")

    def parser_ok(snap, q):
        return [Offer(site="site_ok", price=5000)]

    parser_ok.results_complete = lambda snap: True
    PARSERS["mock_ok"] = parser_ok

    settings = Settings(
        sites=[site1, site2],
        storage_dir=tmp_path,
        tg_allowed_ids={100},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev1", name="Dev 1", status="new")

    async def mock_snapshot(s, device_id, url, **kwargs):
        if "site_ok" in kwargs.get("site_name", ""):
            return Snapshot(url=url, text="OK content with offers", blocked=False), True
        # Второй сайт выдаёт 502
        raise DroidhubError("502 Bad Gateway")

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    report = await run_phone_check(settings, "dev1", bot=fb.bot)
    assert report.admitted is True
    assert "site_ok" in report.ok_sites
    assert "site_fail" in report.failed_sites

    # Реестр обновился: статус active, sites_allow = ["site_ok"]
    dev_entry = fb.bot.phones.get("dev1")
    assert dev_entry["status"] == "active"
    assert dev_entry["sites_allow"] == ["site_ok"]
    assert "site_ok" in dev_entry["note"]


# 6. Если ни один сайт не прошёл проверку, телефон остаётся new
@pytest.mark.asyncio
async def test_phone_check_fails_all_remains_new(tmp_path, monkeypatch):
    site1 = SiteConfig(name="s1", url_template="https://s1", phone="auto", parser="p1")

    def parser_empty(snap, q):
        return []

    PARSERS["p1"] = parser_empty

    settings = Settings(
        sites=[site1],
        storage_dir=tmp_path,
        tg_allowed_ids={100},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_fail", name="Dev Fail", status="new")

    async def mock_snapshot(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="empty page", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    report = await run_phone_check(settings, "dev_fail", bot=fb.bot)
    assert report.admitted is False
    assert fb.bot.phones.get("dev_fail")["status"] == "new"
    assert "пусто" in fb.bot.phones.get("dev_fail")["note"]


# 7. Перепроверка hold-телефона не включает его автоматически
@pytest.mark.asyncio
async def test_recheck_hold_phone_does_not_turn_active(tmp_path, monkeypatch):
    site1 = SiteConfig(name="s1", url_template="https://s1", phone="auto", parser="p_ok")

    def parser_ok(snap, q):
        return [Offer(site="s1", price=5000)]

    parser_ok.results_complete = lambda snap: True
    PARSERS["p_ok"] = parser_ok

    settings = Settings(
        sites=[site1],
        storage_dir=tmp_path,
        tg_allowed_ids={100},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_hold", name="Dev Hold", status="hold")

    async def mock_snapshot(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    await run_phone_check(settings, "dev_hold", bot=fb.bot)
    # Статус остался hold!
    assert fb.bot.phones.get("dev_hold")["status"] == "hold"
    # Но sites_allow обновился
    assert fb.bot.phones.get("dev_hold")["sites_allow"] == ["s1"]


# 8. Ценовая сверка: расхождение > 15% -> не ок, отсутствие эталона -> пропуск
@pytest.mark.asyncio
async def test_price_reference_and_deviation(tmp_path, monkeypatch):
    site1 = SiteConfig(name="s_price", url_template="https://sp", phone="auto", parser="p_dyn")

    # Эталон цены = 10 000 руб
    cur_price = 10000

    def parser_dyn(snap, q):
        return [Offer(site="s_price", price=cur_price)]

    parser_dyn.results_complete = lambda snap: True
    PARSERS["p_dyn"] = parser_dyn

    settings = Settings(
        sites=[site1],
        storage_dir=tmp_path,
        tg_allowed_ids={100},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_p", name="Dev P", status="new")

    async def mock_snapshot(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)

    # 8a. Фиксируем эталон = 10 000 руб.
    # Если телефон возвращает 12 000 руб (+20% > 15%) -> не ок (цена расходится)
    cur_price = 12000
    monkeypatch.setattr("avia.phone_check.get_reference_price", lambda *a, **kw: 10000.0)
    rep = await run_phone_check(settings, "dev_p", bot=fb.bot)
    assert rep.admitted is False
    assert rep.site_results["s_price"].verdict == "не ок"
    assert rep.site_results["s_price"].reason == "цена расходится"

    # 8b. Телефон возвращает 11 000 руб (+10% <= 15%) -> ок
    cur_price = 11000
    rep_ok = await run_phone_check(settings, "dev_p", bot=fb.bot)
    assert rep_ok.admitted is True
    assert rep_ok.site_results["s_price"].verdict == "ок"

    # 8c. Эталон отсутствует (None) -> ценовая сверка пропускается, вердикт ок
    cur_price = 25000
    monkeypatch.setattr("avia.phone_check.get_reference_price", lambda *a, **kw: None)
    fb.bot.phones.set_status("dev_p", "new")
    rep_no_ref = await run_phone_check(settings, "dev_p", bot=fb.bot)
    assert rep_no_ref.admitted is True
    assert rep_no_ref.site_results["s_price"].verdict == "ок"
    assert "эталон отсутствует" in rep_no_ref.site_results["s_price"].price_note


# 9. Телефон не отвечает (все проходы 502) -> ранний выход
@pytest.mark.asyncio
async def test_unresponsive_phone_early_exit(tmp_path, monkeypatch):
    sites = [
        SiteConfig(name=f"site_{i}", url_template=f"https://s{i}", phone="auto")
        for i in range(5)
    ]
    settings = Settings(sites=sites, storage_dir=tmp_path, tg_allowed_ids={100}, phones_new_policy="check")
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_dead", name="Dead Phone", status="new")

    call_count = 0

    async def mock_snapshot_502(s, device_id, url, **kwargs):
        nonlocal call_count
        call_count += 1
        raise DroidhubError("502 Bad Gateway")

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot_502)

    rep = await run_phone_check(settings, "dev_dead", bot=fb.bot)
    assert rep.unresponsive is True
    # Проверка завершилась после 2 проходов первого сайта (не стала опрашивать остальные 4 сайта)
    assert call_count == 2
    assert "не отвечает" in fb.bot.phones.get("dev_dead")["note"]
    assert any("не отвечает, проверьте экран и питание" in m["text"] for m in fb.sent_messages)


# 10. Параллельный повторный запуск не стартует
@pytest.mark.asyncio
async def test_parallel_check_prevented(tmp_path, monkeypatch):
    site = SiteConfig(name="s1", url_template="https://s1", phone="auto")
    reg = PhoneRegistry(tmp_path / "phones.json", phones_new_policy="check")
    reg.register("dev_par", name="Dev Par", status="new")

    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    release_event = asyncio.Event()

    async def slow_snapshot(s, device_id, url, **kwargs):
        await release_event.wait()
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", slow_snapshot)

    task1 = asyncio.create_task(run_phone_check(settings, "dev_par"))
    await asyncio.sleep(0.01)

    assert is_checking("dev_par") is True

    # Вторая попытка проверки того же устройства
    res2 = await run_phone_check(settings, "dev_par")
    assert res2.message == "Уже проверяется"

    release_event.set()
    await task1

    assert is_checking("dev_par") is False


# 11. Запись замеров в site_runs
@pytest.mark.asyncio
async def test_metrics_site_runs_recorded(tmp_path, monkeypatch):
    from avia.metrics import MetricsStore

    metrics_db = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(metrics_db)

    site1 = SiteConfig(name="s_metrics", url_template="https://sm", phone="auto", parser="p_met")

    def parser_met(snap, q):
        return [Offer(site="s_metrics", price=6000)]

    parser_met.results_complete = lambda snap: True
    PARSERS["p_met"] = parser_met

    reg = PhoneRegistry(tmp_path / "phones.json", phones_new_policy="check")
    reg.register("dev_m", name="Dev M", status="new")

    settings = Settings(sites=[site1], storage_dir=tmp_path, phones=reg)

    async def mock_snap(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snap)

    await run_phone_check(settings, "dev_m", metrics=metrics)

    # Проверяем, что в site_runs появились строки с source='phone_check' и device='dev_m'
    with sqlite3.connect(metrics_db) as conn:
        rows = conn.execute(
            "SELECT source, site, device, priority, min_price FROM site_runs WHERE device = ?",
            ("dev_m",),
        ).fetchall()
        assert len(rows) == 5  # PASSES_PER_SITE проходов
        for r in rows:
            assert r[0] == "phone_check"
            assert r[1] == "s_metrics"
            assert r[2] == "dev_m"
            assert r[3] == 0
            assert r[4] == 6000.0


# 12. Отображение «⏳ проверяется» в панели телефонов
def test_checking_status_display_in_admin_panel(tmp_path):
    reg = PhoneRegistry(tmp_path / "phones.json", phones_new_policy="check")
    reg.register("dev_chk", name="Dev Chk", status="new")

    reg.set_checking("dev_chk", True)
    assert reg.is_checking("dev_chk") is True
    assert reg.all()["dev_chk"]["checking"] is True

    text = build_phones({"phones": list(reg.all().values())})
    assert "⏳ проверяется" in text

    reg.set_checking("dev_chk", False)
    assert reg.is_checking("dev_chk") is False
    assert reg.all()["dev_chk"]["checking"] is False
    text_after = build_phones({"phones": list(reg.all().values())})
    assert "ожидает проверки" in text_after


# 13. Общий таймаут проверки: сохранение причины в реестре и отправка отчёта админам
@pytest.mark.asyncio
async def test_phone_check_overall_timeout_notifies_and_records_note(tmp_path, monkeypatch):
    import avia.phone_check as pc

    site = SiteConfig(name="s1", url_template="https://s1", phone="auto")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        tg_allowed_ids={111},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_timeout", name="Dev Timeout", status="new")

    monkeypatch.setattr(pc, "MAX_OVERALL_CHECK_SECONDS", 0.05)

    async def hanging_snapshot(*args, **kwargs):
        await asyncio.sleep(1.0)
        return None, False

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", hanging_snapshot)

    report = await run_phone_check(settings, "dev_timeout", bot=fb.bot, initiated_by_chat_id=222)
    assert "превышен общий лимит времени" in report.message.lower()

    entry = fb.bot.phones.get("dev_timeout")
    assert entry["status"] == "new"
    assert "превышен общий лимит времени" in entry["note"].lower()

    # Админам и инициатору отправлено сообщение
    recipients = {m["chat_id"] for m in fb.sent_messages if "chat_id" in m}
    assert 111 in recipients
    assert 222 in recipients
    assert not is_checking("dev_timeout")


# 14. Необработанное исключение при проверке: сохранение причины и отправка отчёта админам
@pytest.mark.asyncio
async def test_phone_check_exception_notifies_and_records_note(tmp_path, monkeypatch):
    site = SiteConfig(name="s1", url_template="https://s1", phone="auto")
    settings = Settings(
        sites=[site],
        storage_dir=tmp_path,
        tg_allowed_ids={333},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_err", name="Dev Err", status="new")

    async def failing_snapshot(*args, **kwargs):
        raise RuntimeError("Hub crashed unexpectedly")

    monkeypatch.setattr("avia.phone_check._do_run_phone_check", failing_snapshot)

    report = await run_phone_check(settings, "dev_err", bot=fb.bot, initiated_by_chat_id=444)
    assert "Hub crashed unexpectedly" in report.message

    entry = fb.bot.phones.get("dev_err")
    assert entry["status"] == "new"
    assert "Hub crashed unexpectedly" in entry["note"]

    recipients = {m["chat_id"] for m in fb.sent_messages if "chat_id" in m}
    assert 333 in recipients
    assert 444 in recipients
    assert not is_checking("dev_err")


# 15. Сохранение sites_deny при допуске и перепроверке
@pytest.mark.asyncio
async def test_phone_check_preserves_existing_sites_deny(tmp_path, monkeypatch):
    site_ok = SiteConfig(name="site_allow_me", url_template="https://ok", phone="auto", parser="p_ok2")

    def parser_ok(snap, q):
        return [Offer(site="site_allow_me", price=5000)]

    parser_ok.results_complete = lambda snap: True
    PARSERS["p_ok2"] = parser_ok

    settings = Settings(
        sites=[site_ok],
        storage_dir=tmp_path,
        tg_allowed_ids={100},
        phones_new_policy="check",
    )
    fb = FakeBot(settings)
    # Телефон с преднастроенным sites_deny
    fb.bot.phones.register("dev_deny", name="Dev Deny", status="new", sites_deny=["banned_site"])

    async def mock_snap(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snap)

    report = await run_phone_check(settings, "dev_deny", bot=fb.bot)
    assert report.admitted is True

    entry = fb.bot.phones.get("dev_deny")
    assert entry["status"] == "active"
    assert entry["sites_allow"] == ["site_allow_me"]
    # sites_deny НЕ стёрт!
    assert entry["sites_deny"] == ["banned_site"]


# 16. Сетевые ошибки и таймаут проходов не классифицируются ошибочно как «пусто»
def test_evaluate_site_error_distinction():
    # Проходы с таймаутом
    timeout_passes = [
        PassResult(pass_num=1, ok=False, offers_count=0, error="таймаут"),
        PassResult(pass_num=2, ok=False, offers_count=0, error="таймаут"),
    ]
    res_timeout = evaluate_site("s_to", timeout_passes)
    assert res_timeout.verdict == "не ок"
    assert res_timeout.reason == "таймаут"

    # Проходы без парсера
    no_parser_passes = [
        PassResult(pass_num=1, ok=False, offers_count=0, error="нет парсера"),
        PassResult(pass_num=2, ok=False, offers_count=0, error="нет парсера"),
    ]
    res_no_parser = evaluate_site("s_np", no_parser_passes)
    assert res_no_parser.verdict == "не ок"
    assert res_no_parser.reason == "нет парсера"

    # Честное «пусто»: парсер отработал, но офферов нет
    empty_passes = [
        PassResult(pass_num=1, ok=False, offers_count=0, error="пусто"),
        PassResult(pass_num=2, ok=False, offers_count=0, error="пусто"),
    ]
    res_empty = evaluate_site("s_empty", empty_passes)
    assert res_empty.verdict == "не ок"
    assert res_empty.reason == "пусто"


# 17. Эталонная цена фильтрует по source = 'phone_check'
def test_reference_price_filters_by_source_phone_check(tmp_path):
    from avia.metrics import MetricsStore

    metrics_db = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(metrics_db)
    # Инициализируем схему
    metrics._ensure_schema()

    query = Query(
        origin="MOW",
        destination="LED",
        depart=date.today() + timedelta(days=35),
        adults=1,
    )
    now = time.time()

    # Записываем замер от обычного поиска (другой маршрут / другая цена)
    with sqlite3.connect(metrics_db) as conn:
        conn.execute(
            """INSERT INTO site_runs (ts, source, site, ok, partial, captcha, offers, elapsed, min_price)
               VALUES (?, 'search', 's_ref', 1, 0, 0, 10, 2.0, 1000.0)""",
            (now,),
        )

    settings = Settings(sites=[], storage_dir=tmp_path)
    # Замер из 'search' НЕ должен считаться эталоном для проверки телефона
    assert get_reference_price(settings, "s_ref", query, now=now) is None

    # Добавляем замер от 'phone_check'
    with sqlite3.connect(metrics_db) as conn:
        conn.execute(
            """INSERT INTO site_runs (ts, source, site, ok, partial, captcha, offers, elapsed, min_price)
               VALUES (?, 'phone_check', 's_ref', 1, 0, 0, 10, 2.0, 7500.0)""",
            (now,),
        )

    # Теперь эталон равен 7500.0
    assert get_reference_price(settings, "s_ref", query, now=now) == 7500.0


# 18. Колонка min_price присутствует в начальной схеме site_runs
def test_site_runs_initial_schema_has_min_price(tmp_path):
    from avia.metrics import MetricsStore

    metrics_db = tmp_path / "test_initial_schema.sqlite"
    metrics = MetricsStore(metrics_db)
    conn = metrics._connect()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(site_runs)").fetchall()}
        assert "min_price" in cols
        assert "priority" in cols
    finally:
        conn.close()


# 13. Пять проходов на сайт, фоновые замеры полного режима, доля успехов
@pytest.mark.asyncio
async def test_five_passes_and_background_mode(tmp_path, monkeypatch):
    from avia.metrics import MetricsStore

    metrics_db = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(metrics_db)

    site = SiteConfig(
        name="s_full", url_template="https://sf", phone="auto", parser="p_full",
        phone_wait_seconds=10, phone_full_wait_seconds=40, phone_full_wait_for="END",
    )

    def parser_full(snap, q):
        return [Offer(site="s_full", price=5000)]

    parser_full.results_complete = lambda snap: True
    PARSERS["p_full"] = parser_full

    reg = PhoneRegistry(tmp_path / "phones.json", phones_new_policy="check")
    reg.register("dev_f", name="Dev F", status="new")
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    waits: list[int] = []

    async def mock_snap(s, device_id, url, **kwargs):
        waits.append(kwargs.get("wait_seconds"))
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snap)

    report = await run_phone_check(settings, "dev_f", metrics=metrics)

    assert waits == [10] * 5 + [40] * 5
    assert report.site_results["s_full"].verdict == "ок"
    with sqlite3.connect(metrics_db) as conn:
        by_prio = dict(
            conn.execute("SELECT priority, COUNT(*) FROM site_runs WHERE device = 'dev_f' GROUP BY priority").fetchall()
        )
    assert by_prio == {0: 5, 1: 5}
    assert "ок 5/5" in report.message
    assert "полный режим (фон)" in report.message


def test_verdict_by_share_of_good_passes():
    good = PassResult(pass_num=1, ok=True, offers_count=10, min_price=5000, results_complete=True)
    bad = PassResult(pass_num=2, ok=False, error="502")
    assert evaluate_site("s", [good, good, bad, bad, bad]).verdict == "ок"      # 2/5 = 0.4
    assert evaluate_site("s", [good, bad, bad, bad, bad]).verdict == "не ок"    # 1/5


# 9. Перепроверка: passes=1 делает один проход на сайт
@pytest.mark.asyncio
async def test_recheck_single_pass(tmp_path, monkeypatch):
    site1 = SiteConfig(name="s1", url_template="https://s1", phone="auto", parser="p_one")
    calls = []

    def parser_one(snap, q):
        calls.append(1)
        return [Offer(site="s1", price=5000)]

    parser_one.results_complete = lambda snap: True
    PARSERS["p_one"] = parser_one
    settings = Settings(sites=[site1], storage_dir=tmp_path, tg_allowed_ids={100}, phones_new_policy="check")
    fb = FakeBot(settings)
    fb.bot.phones.register("dev_re", name="Dev Re", status="active")

    async def mock_snapshot(s, device_id, url, **kwargs):
        return Snapshot(url=url, text="OK", blocked=False), True

    monkeypatch.setattr("avia.phone_check.fetch_page_snapshot", mock_snapshot)
    await run_phone_check(settings, "dev_re", bot=fb.bot, per_site=1)
    assert len(calls) == 1
    assert fb.bot.phones.get("dev_re")["sites_allow"] == ["s1"]


def test_hit_time_cap_only_for_live_slow_attempts():
    from avia.pipeline import _hit_time_cap

    assert _hit_time_cap(0, {"fetch_seconds": 48.0}, 45) is True
    assert _hit_time_cap(0, {"fetch_seconds": 12.0}, 45) is False
    assert _hit_time_cap(1, {"fetch_seconds": 48.0}, 45) is False
    assert _hit_time_cap(0, {}, 45) is False


def test_hit_time_cap_ignored_when_page_finished():
    from avia.pipeline import _hit_time_cap

    # страница дозагрузилась (finished), но разбор дал 0: повтор на другом телефоне допустим
    assert _hit_time_cap(0, {"fetch_seconds": 48.0, "finished": True}, 45) is False
    assert _hit_time_cap(0, {"fetch_seconds": 48.0, "finished": False}, 45) is True
