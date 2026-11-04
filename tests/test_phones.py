"""Тесты PhoneRegistry, автообнаружения телефонов и расширения маршрутизации."""
from __future__ import annotations

import asyncio
from html import escape
import json
from pathlib import Path
import time
from typing import Any

import pytest

from avia.config import Settings, SiteConfig
from avia.droidhub import PreferenceList, candidates, phone_enabled
from avia.phones import PhoneRegistry


def test_refresh_first_run_silent_and_subsequent_new(tmp_path):
    """Первый запуск с пустым реестром регистрирует устройства молча, последующие выявляют новые."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    assert len(reg) == 0

    # Первый запуск: 2 телефона
    devs_first = [
        {"id": "phone1", "name": "Phone One", "online": True, "battery": 95},
        {"id": "phone2", "name": "Phone Two", "online": True, "battery": 80},
    ]
    new_devs = reg.refresh(devs_first)
    # Первый запуск молча: список пуст
    assert new_devs == []
    assert len(reg) == 2
    assert "phone1" in reg
    assert "phone2" in reg
    assert reg.get("phone1")["status"] == "active"
    assert reg.get("phone1")["name"] == "Phone One"

    # Повторный опрос тех же устройств: новых нет
    assert reg.refresh(devs_first) == []

    # Добавляется третий телефон
    devs_second = devs_first + [
        {"id": "phone3", "name": "Phone Three", "online": True, "battery": 70}
    ]
    new_second = reg.refresh(devs_second)
    assert len(new_second) == 1
    assert new_second[0] == "phone3"
    assert new_second[0].id == "phone3"
    assert new_second[0].name == "Phone Three"
    assert new_second[0].status == "active"
    assert len(reg) == 3

    # Проверка персистентности: повторная загрузка с диска
    reg_reloaded = PhoneRegistry(tmp_path / "phones.json")
    assert len(reg_reloaded) == 3
    assert "phone3" in reg_reloaded


def test_refresh_policy_hold(tmp_path):
    """При политике phones_new_policy='hold' новые телефоны регистрируются со статусом hold."""
    reg = PhoneRegistry(tmp_path / "phones.json", phones_new_policy="hold")
    reg.refresh([{"id": "hold_dev", "name": "Hold Dev"}])
    assert reg.get("hold_dev")["status"] == "hold"
    assert not reg.is_usable("hold_dev")


def test_is_usable_hold_active_and_status_toggle(tmp_path):
    """is_usable учитывает active/hold статус и реагирует на set_status."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Dev 1", status="active")
    assert reg.is_usable("dev1") is True

    # Заморозка
    assert reg.set_status("dev1", "hold") is True
    assert reg.is_usable("dev1") is False

    # Разморозка
    assert reg.set_status("dev1", "active") is True
    assert reg.is_usable("dev1") is True

    # Несуществующее устройство
    assert reg.is_usable("unknown") is False
    assert reg.set_status("unknown", "active") is False


def test_is_usable_allow_and_deny_sites(tmp_path):
    """is_usable фильтрует по sites_allow и sites_deny."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Dev 1", status="active")

    # Без ограничений: любой сайт разрешён
    assert reg.is_usable("dev1", "aviasales") is True
    assert reg.is_usable("dev1", "tutu") is True

    # sites_allow
    reg.set_sites("dev1", allow=["aviasales", "tutu"])
    assert reg.is_usable("dev1", "aviasales") is True
    assert reg.is_usable("dev1", "tutu") is True
    assert reg.is_usable("dev1", "yandex") is False

    # sites_deny
    reg.set_sites("dev1", allow=None, deny=["yandex"])
    assert reg.is_usable("dev1", "aviasales") is True
    assert reg.is_usable("dev1", "yandex") is False


def test_is_usable_phones_hold_env(tmp_path, monkeypatch):
    """Переменная окружения PHONES_HOLD жёстко замораживает устройство поверх реестра."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("dev1", name="Dev 1", status="active")
    reg.register("dev2", name="Dev 2", status="active")

    monkeypatch.setenv("PHONES_HOLD", "dev1,other")
    assert reg.is_usable("dev1") is False
    assert reg.is_usable("dev2") is True


def test_is_usable_snapshot_online_and_expiry(tmp_path):
    """Свежий офлайн-снимок исключает телефон, устаревший (>180 с) считается онлайн."""
    reg = PhoneRegistry(tmp_path / "phones.json", phones_snapshot_ttl_seconds=180)
    reg.register("dev1", name="Dev 1", status="active")

    # Свежий снимок: онлайн
    reg.update_snapshot("dev1", online=True, updated_at=time.monotonic())
    assert reg.is_usable("dev1") is True

    # Свежий снимок: офлайн
    reg.update_snapshot("dev1", online=False, updated_at=time.monotonic())
    assert reg.is_usable("dev1") is False

    # Устаревший снимок (старше 180 с): считаем онлайн, фильтр не применяется
    reg.update_snapshot("dev1", online=False, updated_at=time.monotonic() - 250)
    assert reg.is_usable("dev1") is True


def test_corrupted_phones_json_resilience(tmp_path):
    """Повреждённый файл phones.json не вызывает падения, реестр стартует пустым."""
    phones_file = tmp_path / "phones.json"
    phones_file.write_text("{broken json: 123", encoding="utf-8")

    reg = PhoneRegistry(phones_file)
    assert len(reg) == 0
    # Работает корректно после сброса
    reg.refresh([{"id": "new1", "name": "New"}])
    assert "new1" in reg


def test_candidates_with_extension(tmp_path):
    """Кандидаты для «a,b» расширяются остальными пригодными телефонами из реестра."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    # Регистрируем телефоны с фиксированным first_seen
    reg.register("redmi", name="Redmi", status="active", first_seen=10.0)
    reg.register("realme", name="Realme", status="active", first_seen=20.0)
    reg.register("pixel", name="Pixel", status="active", first_seen=30.0)
    reg.register("samsung", name="Samsung", status="active", first_seen=40.0)

    site = SiteConfig(name="as", url_template="https://test", phone="redmi,realme", phone_patience_seconds=5)
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    cands = candidates(settings, site)
    # Основные из site.phone + запасные из реестра по first_seen
    assert list(cands) == ["redmi", "realme", "pixel", "samsung"]
    assert isinstance(cands, PreferenceList)
    assert cands.patience == 5.0


def test_candidates_frozen_phone_excluded(tmp_path, monkeypatch):
    """Замороженный телефон (через status=hold или PHONES_HOLD) исключается из кандидатов."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("redmi", name="Redmi", status="active", first_seen=10.0)
    reg.register("realme", name="Realme", status="hold", first_seen=20.0)  # hold в реестре
    reg.register("pixel", name="Pixel", status="active", first_seen=30.0)

    monkeypatch.setenv("PHONES_HOLD", "pixel")  # pixel в PHONES_HOLD

    site = SiteConfig(name="as", url_template="https://test", phone="redmi,realme")
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    cands = candidates(settings, site)
    # realme (hold) и pixel (PHONES_HOLD) исключены
    assert list(cands) == ["redmi"]


def test_candidates_single_phone_not_extended(tmp_path):
    """Одиночный phone: 'realme' остаётся жёстко закреплённым и не расширяется."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("realme", name="Realme", status="active", first_seen=10.0)
    reg.register("pixel", name="Pixel", status="active", first_seen=20.0)

    site = SiteConfig(name="single_site", url_template="https://test", phone="realme")
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    cands = candidates(settings, site)
    assert cands == ["realme"]

    # При заморозке одиночного телефона phone_enabled=False и candidates=[]
    reg.set_status("realme", "hold")
    assert candidates(settings, site) == []
    settings.droidhub_url = "https://hub.test"
    settings.droidhub_token = "tok"
    assert phone_enabled(settings, site) is False


def test_candidates_phone_extend_false(tmp_path):
    """Флаг phone_extend: false отключает добавление запасных телефонов из реестра."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("redmi", name="Redmi", status="active", first_seen=10.0)
    reg.register("realme", name="Realme", status="active", first_seen=20.0)
    reg.register("pixel", name="Pixel", status="active", first_seen=30.0)

    site = SiteConfig(
        name="as",
        url_template="https://test",
        phone="redmi,realme",
        phone_extend=False,
    )
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    cands = candidates(settings, site)
    assert list(cands) == ["redmi", "realme"]


def test_candidates_empty_registry_backward_compatibility():
    """При пустом или недоступном реестре сохраняется прежнее поведение."""
    site_list = SiteConfig(name="as", url_template="https://test", phone="redmi,realme")
    site_single = SiteConfig(name="as", url_template="https://test", phone="realme")
    site_auto = SiteConfig(name="as", url_template="https://test", phone="auto")

    settings = Settings(
        sites=[site_list, site_single, site_auto],
        droidhub_devices=["devA", "devB"],
        phones=None,
    )

    assert list(candidates(settings, site_list)) == ["redmi", "realme"]
    assert candidates(settings, site_single) == ["realme"]
    assert candidates(settings, site_auto) == ["devA", "devB"]


def test_site_config_phone_extend_validation():
    """SiteConfig проверяет булев тип phone_extend."""
    s1 = SiteConfig(name="s1", url_template="https://t", phone_extend=True)
    assert s1.phone_extend is True

    s2 = SiteConfig(name="s2", url_template="https://t", phone_extend=False)
    assert s2.phone_extend is False

    with pytest.raises(TypeError, match="phone_extend must be a boolean"):
        SiteConfig(name="s3", url_template="https://t", phone_extend="invalid")  # type: ignore


# ---------------------------------------------------------------------------
# Тесты интеграции с ботом: автообнаружение, уведомления, панель /admin и /phone
# ---------------------------------------------------------------------------


class FakeBot:
    """Фейковый бот для тестирования команд и панели без сети."""

    def __init__(self, settings: Settings):
        from avia.bot import Bot

        self.bot = Bot(settings)
        self.bot.call = self._mock_call  # type: ignore
        self.sent_messages: list[dict[str, Any]] = []

    async def _mock_call(self, method: str, **kwargs: Any) -> Any:
        if method == "sendMessage":
            self.sent_messages.append(kwargs)
            return {"message_id": 100, **kwargs}
        if method == "editMessageText":
            self.sent_messages.append(kwargs)
            return {"message_id": kwargs.get("message_id", 100), **kwargs}
        if method == "answerCallbackQuery":
            return True
        return {}


@pytest.mark.asyncio
async def test_hub_autodiscovery_failure_does_not_crash(tmp_path, monkeypatch):
    """Ошибка запроса хаба не роняет автообнаружение и не трогает реестр."""
    settings = Settings(
        sites=[SiteConfig(name="as", url_template="https://test")],
        droidhub_url="https://hub.example.com",
        droidhub_token="fake_token",
        tg_token="fake_tg",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
    )
    fb = FakeBot(settings)

    async def mock_fetch_fail():
        raise RuntimeError("Hub network timeout 502")

    monkeypatch.setattr(fb.bot, "_fetch_hub_devices", mock_fetch_fail)

    res = await fb.bot._check_hub_devices_once()
    assert res == []
    assert fb.bot._phones_fail_count == 1
    assert len(fb.sent_messages) == 0


@pytest.mark.asyncio
async def test_hub_autodiscovery_notifies_admin_on_new_phone(tmp_path, monkeypatch):
    """Обнаружение нового телефона отправляет админам сообщение с кнопками управления."""
    settings = Settings(
        sites=[SiteConfig(name="as", url_template="https://test")],
        droidhub_url="https://hub.example.com",
        droidhub_token="fake_token",
        tg_token="fake_tg",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
    )
    fb = FakeBot(settings)

    # Первый запуск — устройство записывается молча
    monkeypatch.setattr(
        fb.bot,
        "_fetch_hub_devices",
        lambda: asyncio.sleep(0, result=[{"id": "p1", "name": "Phone One", "online": True}]),
    )
    await fb.bot._check_hub_devices_once()
    assert len(fb.sent_messages) == 0

    # Второй опрос: появляется p2 — админу отправляется оповещение
    monkeypatch.setattr(
        fb.bot,
        "_fetch_hub_devices",
        lambda: asyncio.sleep(
            0,
            result=[
                {"id": "p1", "name": "Phone One", "online": True},
                {"id": "p2", "name": "Phone Two", "online": True},
            ],
        ),
    )
    await fb.bot._check_hub_devices_once()
    assert len(fb.sent_messages) == 1
    msg_text = fb.sent_messages[0]["text"]
    assert "Новый телефон в хабе" in msg_text
    assert "Phone Two" in msg_text
    assert "p2" in msg_text
    assert "взят в работу" in msg_text


@pytest.mark.asyncio
async def test_admin_phone_command_and_permissions(tmp_path):
    """Команда /phone доступна только администраторам и подтверждает действия."""
    site1 = SiteConfig(name="aviasales", url_template="https://as")
    site2 = SiteConfig(name="tutu", url_template="https://tutu")
    settings = Settings(
        sites=[site1, site2],
        tg_token="fake",
        tg_allowed_ids={100},
        storage_dir=tmp_path,
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("dev1", name="Dev One", status="active")

    # 1. Не-администратор (user_id 999) -> команда игнорируется
    msg_stranger = {
        "chat": {"id": 999, "type": "private"},
        "from": {"id": 999},
        "text": "/phone dev1 hold",
    }
    await fb.bot.handle(msg_stranger)
    assert len(fb.sent_messages) == 0
    assert fb.bot.phones.get("dev1")["status"] == "active"

    # 2. Администратор: /phone dev1 hold
    msg_admin_hold = {
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 100},
        "text": "/phone dev1 hold",
    }
    await fb.bot.handle(msg_admin_hold)
    assert len(fb.sent_messages) == 1
    assert "заморожен" in fb.sent_messages[-1]["text"]
    assert fb.bot.phones.get("dev1")["status"] == "hold"

    # 3. Администратор: /phone dev1 on
    msg_admin_on = {
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 100},
        "text": "/phone dev1 on",
    }
    await fb.bot.handle(msg_admin_on)
    assert "в работе" in fb.sent_messages[-1]["text"]
    assert fb.bot.phones.get("dev1")["status"] == "active"

    # 4. Администратор: /phone dev1 allow с неизвестным сайтом
    msg_admin_bad_site = {
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 100},
        "text": "/phone dev1 allow unknown_site",
    }
    await fb.bot.handle(msg_admin_bad_site)
    assert "Неизвестные сайты" in fb.sent_messages[-1]["text"]

    # 5. Администратор: /phone dev1 allow aviasales
    msg_admin_allow = {
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 100},
        "text": "/phone dev1 allow aviasales",
    }
    await fb.bot.handle(msg_admin_allow)
    assert "разрешены сайты:" in fb.sent_messages[-1]["text"]
    assert "aviasales" in fb.sent_messages[-1]["text"]
    assert fb.bot.phones.get("dev1")["sites_allow"] == ["aviasales"]

    # 6. Администратор: /phone dev1 all
    msg_admin_all = {
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 100},
        "text": "/phone dev1 all",
    }
    await fb.bot.handle(msg_admin_all)
    assert "все сайты" in fb.sent_messages[-1]["text"]
    assert fb.bot.phones.get("dev1")["sites_allow"] is None


@pytest.mark.asyncio
async def test_admin_panel_phones_section_and_callbacks(tmp_path):
    """Раздел Телефоны в /admin отображает список и обрабатывает коллбэки заморозки."""
    settings = Settings(
        sites=[SiteConfig(name="as", url_template="https://as")],
        tg_token="fake",
        tg_allowed_ids={100},
        storage_dir=tmp_path,
    )
    fb = FakeBot(settings)
    fb.bot.phones.register("d1", name="Device 1", status="active")

    # Открытие раздела телефонов
    cq_open = {
        "id": "cq1",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 50},
        "data": "adm:ph",
    }
    await fb.bot.handle_callback(cq_open)
    assert len(fb.sent_messages) == 1
    panel_text = fb.sent_messages[-1]["text"]
    assert "Телефоны хаба" in panel_text
    assert "Device 1" in panel_text
    assert "в работе" in panel_text

    # Нажатие «Заморозить»
    cq_hold = {
        "id": "cq2",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 50},
        "data": "adm:ph:hold:d1",
    }
    await fb.bot.handle_callback(cq_hold)
    assert fb.bot.phones.get("d1")["status"] == "hold"

    # Нажатие «В работу»
    cq_on = {
        "id": "cq3",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 50},
        "data": "adm:ph:on:d1",
    }
    await fb.bot.handle_callback(cq_on)
    assert fb.bot.phones.get("d1")["status"] == "active"

    # Телефон с сетевым adb id (ip:port с двоеточием)
    fb.bot.phones.register("192.168.1.100:5555", name="Net Phone", status="active")
    cq_hold_net = {
        "id": "cq4",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 50},
        "data": "adm:ph:hold:192.168.1.100:5555",
    }
    await fb.bot.handle_callback(cq_hold_net)
    assert fb.bot.phones.get("192.168.1.100:5555")["status"] == "hold"

    cq_on_net = {
        "id": "cq5",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 50},
        "data": "adm:ph:on:192.168.1.100:5555",
    }
    await fb.bot.handle_callback(cq_on_net)
    assert fb.bot.phones.get("192.168.1.100:5555")["status"] == "active"


def test_refresh_empty_first_response_does_not_break_silence(tmp_path):
    """Пустой первый ответ хаба не должен снимать флаг первого молчаливого добавления."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    # 1. Первый refresh возвращает пустой список устройств (хаб пуст / не инициализирован)
    assert reg.refresh([]) == []
    assert len(reg) == 0

    # 2. Второй refresh возвращает первые устройства - они должны зарегистрироваться молча
    devs = [
        {"id": "p1", "name": "Phone One", "online": True},
        {"id": "p2", "name": "Phone Two", "online": True},
    ]
    assert reg.refresh(devs) == []
    assert len(reg) == 2

    # 3. Третий refresh добавляет новое устройство p3 - вот оно уже возвращается как NewDevice
    devs3 = devs + [{"id": "p3", "name": "Phone Three", "online": True}]
    new3 = reg.refresh(devs3)
    assert len(new3) == 1
    assert new3[0].id == "p3"


def test_candidates_unusable_listed_phone_not_in_others(tmp_path):
    """Телефон, указанный в site.phone, но непригодный (hold/offline), не попадает в запасные (others)."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("redmi", name="Redmi", status="active", first_seen=10.0)
    reg.register("realme", name="Realme", status="hold", first_seen=20.0)  # в site.phone, но hold
    reg.register("pixel", name="Pixel", status="active", first_seen=30.0)   # запасной из реестра

    site = SiteConfig(name="as", url_template="https://test", phone="redmi,realme")
    settings = Settings(sites=[site], storage_dir=tmp_path, phones=reg)

    cands = candidates(settings, site)
    # realme не должен появиться в конце как запасной
    assert list(cands) == ["redmi", "pixel"]

    # Аналогично для auto
    site_auto = SiteConfig(name="as_auto", url_template="https://test", phone="auto")
    settings_auto = Settings(
        sites=[site_auto],
        storage_dir=tmp_path,
        phones=reg,
        droidhub_devices=["redmi", "realme"],
    )
    cands_auto = candidates(settings_auto, site_auto)
    assert list(cands_auto) == ["redmi", "pixel"]


def test_save_locked_cleans_up_tmp_file_on_error(tmp_path, monkeypatch):
    """При ошибке записи временный файл .tmp удаляется и не остаётся на диске."""
    reg = PhoneRegistry(tmp_path / "phones.json")
    reg.register("p1", name="Phone 1")

    # Имитируем ошибку при json.dump
    def mock_dump(*args, **kwargs):
        raise OSError("Disk full simulation")

    monkeypatch.setattr(json, "dump", mock_dump)
    reg._save_locked()

    # Проверяем, что временных файлов .tmp в директории нет
    tmp_files = list(tmp_path.glob("phones.*.tmp"))
    assert tmp_files == []
