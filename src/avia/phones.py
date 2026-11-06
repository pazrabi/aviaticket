"""Реестр телефонов хаба: хранение, пригодность для сайтов, автообнаружение."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class NewDevice(str):
    """Информация о новом зарегистрированном устройстве (строка-id с доступом к полям)."""
    id: str
    name: str
    status: str

    def __new__(cls, dev_id: str, name: str = "", status: str = ""):
        clean_id = str(dev_id).strip()
        obj = super().__new__(cls, clean_id)
        obj.id = clean_id
        obj.name = str(name).strip() if name else clean_id
        obj.status = str(status).strip() or "active"
        return obj

    def __getitem__(self, key: Any) -> Any:
        if key == "id":
            return self.id
        if key == "name":
            return self.name
        if key == "status":
            return self.status
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "id":
            return self.id
        if key == "name":
            return self.name
        if key == "status":
            return self.status
        return default


@dataclass
class PhoneSnapshot:
    """Снимок состояния устройства из хаба (хранится только в памяти)."""
    online: bool = True
    battery: int | None = None
    charging: bool = False
    last_seen: Any = None
    busy: bool = False
    temperature: Any = None
    screen_on: bool | None = None
    activity: str = ""
    error: str = ""
    updated_at: float = field(default_factory=time.monotonic)


class PhoneRegistry:
    """Хранит реестр телефонов в storage_dir/phones.json и снимки состояния в памяти."""

    def __init__(
        self,
        storage_dir: Path | str | None = None,
        *,
        path: Path | str | None = None,
        phones_new_policy: str = "check",
        phones_snapshot_ttl_seconds: float = 180.0,
        phones_hold: list[str] | set[str] | str | None = None,
        phone_min_battery: int | None = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
            self.storage_dir = self.path.parent
        elif storage_dir is not None:
            p = Path(storage_dir)
            if p.suffix == ".json":
                self.path = p
                self.storage_dir = p.parent
            else:
                self.storage_dir = p
                self.path = p / "phones.json"
        else:
            self.storage_dir = Path("state")
            self.path = self.storage_dir / "phones.json"

        pol = (phones_new_policy or "check").strip().lower()
        if pol in ("active", "hold", "check", "new"):
            self.phones_new_policy = pol
        else:
            self.phones_new_policy = "check"
        self.phones_snapshot_ttl_seconds = float(phones_snapshot_ttl_seconds)
        self._phones_hold = self._parse_hold(phones_hold)
        if phone_min_battery is not None:
            self.phone_min_battery = int(phone_min_battery)
        else:
            try:
                self.phone_min_battery = int(os.getenv("PHONE_MIN_BATTERY", "15"))
            except ValueError:
                self.phone_min_battery = 15

        self._lock = threading.Lock()
        self._phones: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, PhoneSnapshot] = {}
        self._checking: set[str] = set()

        # Флаг первого обновления: если файл отсутствовал или был пуст на старте,
        # первый вызов refresh регистрирует телефоны молча.
        self._is_first_refresh: bool = False
        self._load()

    @staticmethod
    def _parse_hold(hold: list[str] | set[str] | str | None) -> set[str]:
        if hold is None:
            return set()
        if isinstance(hold, str):
            return {x.strip() for x in hold.split(",") if x.strip()}
        return {str(x).strip() for x in hold if str(x).strip()}

    def _is_held_by_env(self, dev_id: str) -> bool:
        env_val = os.getenv("PHONES_HOLD")
        if env_val is not None:
            env_hold = {x.strip() for x in env_val.split(",") if x.strip()}
            if dev_id in env_hold:
                return True
        if dev_id in self._phones_hold:
            return True
        return False

    def _load(self) -> None:
        """Загружает реестр из phones.json (повреждённый/отсутствующий = пустой реестр)."""
        with self._lock:
            if not self.path.exists():
                self._phones = {}
                self._is_first_refresh = True
                return

            try:
                content = self.path.read_text(encoding="utf-8")
                data = json.loads(content)
                if isinstance(data, dict):
                    self._phones = data
                    self._is_first_refresh = len(self._phones) == 0
                else:
                    logger.warning("phones.json не содержит dict, сброс в пустой реестр")
                    self._phones = {}
                    self._is_first_refresh = True
            except Exception as e:
                logger.warning("Ошибка чтения phones.json (%s), сброс в пустой реестр", e)
                self._phones = {}
                self._is_first_refresh = True

    def _save_locked(self) -> None:
        """Сохраняет self._phones в phones.json через временный файл."""
        tmp_path = self.storage_dir / f"phones.{uuid.uuid4().hex}.tmp"
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._phones, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.warning("Ошибка сохранения phones.json: %s", e)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def refresh(self, devices: list[dict[str, Any]]) -> list[NewDevice]:
        """Обновляет состояние устройств и регистрирует новые.

        Возвращает список NewDevice для устройств, впервые появившихся в хабе
        (при первом запуске с пустым реестром возвращает [], регистрируя молча).
        """
        now = time.time()
        now_mono = time.monotonic()
        new_devices: list[NewDevice] = []

        with self._lock:
            is_first = self._is_first_refresh and len(self._phones) == 0

            changed = False
            for dev in devices:
                if not isinstance(dev, dict):
                    continue
                dev_id = str(dev.get("id", "")).strip()
                if not dev_id:
                    continue
                name = str(dev.get("name", "")).strip() or dev_id
                charging = bool(dev.get("charging") or dev.get("plugged"))

                # Обновляем снимок состояния только в памяти
                snap = PhoneSnapshot(
                    online=bool(dev.get("online", True)),
                    battery=dev.get("battery"),
                    charging=charging,
                    last_seen=dev.get("last_seen"),
                    busy=bool(dev.get("busy", False)),
                    temperature=dev.get("temperature"),
                    screen_on=dev.get("screen_on"),
                    activity=str(dev.get("activity", "")),
                    error=str(dev.get("error", "")),
                    updated_at=now_mono,
                )
                self._snapshots[dev_id] = snap

                if dev_id not in self._phones:
                    # Новый телефон в реестре
                    init_status = "new" if self.phones_new_policy in ("check", "new") else self.phones_new_policy
                    entry = {
                        "name": name,
                        "first_seen": now,
                        "status": init_status,
                        "sites_allow": None,
                        "sites_deny": [],
                        "note": "",
                        "probe_at": {},
                    }
                    self._phones[dev_id] = entry
                    changed = True
                    if not is_first:
                        new_devices.append(NewDevice(dev_id, name, init_status))
                else:
                    # Существующий телефон: обновляем name при изменении
                    entry = self._phones[dev_id]
                    if entry.get("name") != name:
                        entry["name"] = name
                        changed = True

            if changed and self._is_first_refresh:
                self._is_first_refresh = False

            if changed:
                self._save_locked()

        return new_devices

    def update_snapshot(
        self,
        id: str,
        online: bool = True,
        battery: int | None = None,
        charging: bool = False,
        updated_at: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Обновляет снимок состояния в памяти (для тестов или прямого обновления)."""
        clean_id = str(id).strip()
        with self._lock:
            self._snapshots[clean_id] = PhoneSnapshot(
                online=online,
                battery=battery,
                charging=charging,
                updated_at=updated_at if updated_at is not None else time.monotonic(),
                **kwargs,
            )

    def is_usable(
        self,
        id: str,
        site_name: str | None = None,
        *,
        ignore_battery: bool = False,
    ) -> bool:
        """True, если телефон пригоден для работы на указанном сайте.

        Условия:
        1. Не в PHONES_HOLD (жёсткая заморозка перекрывает реестр).
        2. Телефон есть в реестре со статусом active.
        3. site_name проходит sites_allow/sites_deny (если заданы).
        4. Свежий снимок состояния не помечен офлайн (старый снимок или его отсутствие = онлайн).
        5. Батарея не ниже phone_min_battery (если не на зарядке).
        """
        clean_id = str(id).strip()
        if not clean_id:
            return False

        if self._is_held_by_env(clean_id):
            return False

        if self.is_checking(clean_id):
            return False

        with self._lock:
            entry = self._phones.get(clean_id)
            if entry is None:
                return False

            if entry.get("status") != "active":
                return False

            if site_name:
                allow = entry.get("sites_allow")
                if allow is not None and site_name not in allow:
                    return False
                deny = entry.get("sites_deny") or []
                if site_name in deny:
                    return False

            snap = self._snapshots.get(clean_id)
            if snap is not None:
                age = time.monotonic() - snap.updated_at
                if age <= self.phones_snapshot_ttl_seconds:
                    if not snap.online:
                        return False
                    if (
                        not ignore_battery
                        and self.phone_min_battery > 0
                        and snap.battery is not None
                        and snap.battery < self.phone_min_battery
                        and not snap.charging
                    ):
                        return False

        return True

    def set_status(self, id: str, status: str) -> bool:
        """Устанавливает статус устройства ('active', 'hold' или 'new')."""
        clean_id = str(id).strip()
        clean_status = str(status).strip().lower()
        if clean_status not in ("active", "hold", "new"):
            raise ValueError(f"Недопустимый статус: {status}")
        with self._lock:
            entry = self._phones.get(clean_id)
            if entry is None:
                return False
            entry["status"] = clean_status
            self._save_locked()
            return True

    def set_note(self, id: str, note: str) -> bool:
        """Устанавливает примечание/причину отказа."""
        clean_id = str(id).strip()
        with self._lock:
            entry = self._phones.get(clean_id)
            if entry is None:
                return False
            entry["note"] = str(note)
            self._save_locked()
            return True

    def is_checking(self, id: str) -> bool:
        """True, если устройство сейчас проходит проверку."""
        clean_id = str(id).strip()
        with self._lock:
            return clean_id in self._checking

    def set_checking(self, id: str, val: bool) -> None:
        """Устанавливает состояние прохождения проверки."""
        clean_id = str(id).strip()
        with self._lock:
            if val:
                self._checking.add(clean_id)
            else:
                self._checking.discard(clean_id)

    def set_sites(
        self,
        id: str,
        allow: list[str] | None | object = _UNSET,
        deny: list[str] | None = None,
    ) -> bool:
        """Устанавливает списки разрешённых/запрещённых сайтов."""
        clean_id = str(id).strip()
        with self._lock:
            entry = self._phones.get(clean_id)
            if entry is None:
                return False
            if allow is not _UNSET:
                if allow is not None:
                    entry["sites_allow"] = [str(s).strip() for s in allow if str(s).strip()]
                else:
                    entry["sites_allow"] = None
            if deny is not None:
                entry["sites_deny"] = [str(s).strip() for s in deny if str(s).strip()]
            self._save_locked()
            return True

    def get_probe_at(self, id: str, site_name: str | None = None) -> Any:
        """Возвращает время последней пробы сайта на телефоне или словарь {site: ts}."""
        clean_id = str(id).strip()
        with self._lock:
            entry = self._phones.get(clean_id)
            if entry is None:
                return None if site_name is not None else {}
            probes = entry.get("probe_at")
            if not isinstance(probes, dict):
                probes = {}
            if site_name is not None:
                return probes.get(str(site_name).strip())
            return dict(probes)

    def set_probe_at(self, id: str, site_name: str, ts: float | None = None) -> bool:
        """Устанавливает время последней пробы пары (сайт, телефон) и сохраняет на диск."""
        clean_id = str(id).strip()
        clean_site = str(site_name).strip()
        if not clean_id or not clean_site:
            return False
        if ts is None:
            ts_val = time.time()
        else:
            ts_val = float(ts)
        with self._lock:
            entry = self._phones.get(clean_id)
            if entry is None:
                return False
            if "probe_at" not in entry or not isinstance(entry["probe_at"], dict):
                entry["probe_at"] = {}
            entry["probe_at"][clean_site] = ts_val
            self._save_locked()
            return True

    def register(
        self,
        id: str,
        name: str = "",
        status: str | None = None,
        sites_allow: list[str] | None = None,
        sites_deny: list[str] | None = None,
        note: str = "",
        first_seen: float | None = None,
        probe_at: dict[str, float] | None = None,
    ) -> None:
        """Регистрирует устройство напрямую в реестре (для инициализации и тестов)."""
        clean_id = str(id).strip()
        with self._lock:
            init_status = "new" if self.phones_new_policy in ("check", "new") else self.phones_new_policy
            self._phones[clean_id] = {
                "name": name.strip() or clean_id,
                "first_seen": first_seen if first_seen is not None else time.time(),
                "status": (status or init_status).strip().lower(),
                "sites_allow": sites_allow,
                "sites_deny": sites_deny or [],
                "note": note,
                "probe_at": dict(probe_at) if probe_at else {},
            }
            self._is_first_refresh = False
            self._save_locked()

    def all(self) -> dict[str, dict[str, Any]]:
        """Возвращает все устройства реестра с объединёнными данными снимка состояния."""
        with self._lock:
            now_mono = time.monotonic()
            res = {}
            for dev_id, entry in self._phones.items():
                snap = self._snapshots.get(dev_id)
                item = dict(entry)
                item["id"] = dev_id
                item["is_hold_env"] = self._is_held_by_env(dev_id)
                item["note"] = entry.get("note", "")
                item["checking"] = dev_id in self._checking
                item["probe_at"] = dict(entry.get("probe_at") or {})
                if snap is not None:
                    age = now_mono - snap.updated_at
                    item["online"] = snap.online
                    item["is_online_fresh"] = age <= self.phones_snapshot_ttl_seconds
                    item["battery"] = snap.battery
                    item["charging"] = snap.charging
                    item["low_battery"] = bool(
                        self.phone_min_battery > 0
                        and snap.battery is not None
                        and snap.battery < self.phone_min_battery
                        and not snap.charging
                    )
                    item["last_seen"] = snap.last_seen
                    item["busy"] = snap.busy
                    item["temperature"] = snap.temperature
                else:
                    item["online"] = None
                    item["is_online_fresh"] = False
                    item["battery"] = None
                    item["charging"] = False
                    item["low_battery"] = False
                    item["last_seen"] = None
                    item["busy"] = None
                    item["temperature"] = None
                res[dev_id] = item
            return res

    def get(self, id: str) -> dict[str, Any] | None:
        """Возвращает данные конкретного устройства или None."""
        clean_id = str(id).strip()
        return self.all().get(clean_id)

    def __contains__(self, id: str) -> bool:
        with self._lock:
            return str(id).strip() in self._phones

    def __len__(self) -> int:
        with self._lock:
            return len(self._phones)
