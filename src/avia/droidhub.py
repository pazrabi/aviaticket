"""Интеграция с сервисом droidhub для сбора страниц через Android-телефон."""
from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass
import inspect
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

import httpx

from .collector import Snapshot
from .config import Settings, SiteConfig

logger = logging.getLogger(__name__)


class PreferenceList(list):
    """Список устройств с фиксированным порядком предпочтения (не round-robin)."""
    prefer_order: bool = True
    patience: float = 0.0
    preferred_count: int = 1

    def __init__(
        self,
        iterable: Any = (),
        *,
        patience: float | None = None,
        preferred_count: int | None = None,
    ) -> None:
        super().__init__(iterable)
        if patience is not None:
            self.patience = float(patience)
        if preferred_count is not None:
            self.preferred_count = int(preferred_count)


@dataclass
class _Waiter:
    priority: int
    weight: int
    seq: int
    candidates: list[str]
    prefer_order: bool
    fut: asyncio.Future[str]
    patience: float = 0.0
    deadline: float = 0.0
    timer_handle: asyncio.TimerHandle | None = None
    preferred_count: int = 1

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.priority, -self.weight, self.seq)


# Блокировки по device_id для сериализации запросов к каждому конкретному телефону
_device_locks: dict[str, asyncio.Lock] = {}

# Состояние пула устройств
_busy_devices: set[str] = set()
_pool_condition: asyncio.Condition | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None
_rr_counter: int = 0
_seq_counter: int = 0
_waiters: list[_Waiter] = []
_clock: Callable[[], float] | None = None


def _time() -> float:
    """Возвращает текущее время (монотонное или кастомные часы)."""
    if _clock is not None:
        return _clock()
    return time.monotonic()


def set_clock(clock: Callable[[], float] | None) -> None:
    """Устанавливает функцию часов для тестов."""
    global _clock
    _clock = clock


def advance_time(seconds: float) -> None:
    """Продвигает виртуальное время и вызывает диспетчер (для тестов)."""
    global _clock
    if _clock is not None and hasattr(_clock, "advance"):
        _clock.advance(seconds)
    _dispatch()


class DroidhubError(Exception):
    """Исключение при ошибках обращения к сервису droidhub."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class JobsUnsupported(DroidhubError):
    """Хаб не поддерживает задания fetch_jobs (404/405)."""


_jobs_unsupported: bool = False
# Сайты, где задание хаба не отдало ни одного ответа (а старый fetch_page отдаёт): {сайт: до какого времени без заданий}
_jobs_bad_sites: dict[str, float] = {}
JOBS_BAD_SITE_SECONDS: float = 1800.0
POLL_MIN_INTERVAL: float = 0.3
DELETE_TIMEOUT: float = 3.0


def reset_jobs_supported() -> None:
    """Сброс флага поддержки fetch_jobs (для изоляции тестов)."""
    global _jobs_unsupported
    _jobs_unsupported = False
    _jobs_bad_sites.clear()


def _sync_loop_state() -> asyncio.AbstractEventLoop | None:
    """Синхронизирует блокировки и состояние пула с текущим циклом событий."""
    global _pool_condition, _pool_loop, _busy_devices, _device_locks, _seq_counter, _waiters
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _pool_loop is not loop:
        _pool_condition = None
        _pool_loop = loop
        _busy_devices.clear()
        _device_locks.clear()
        _seq_counter = 0
        _waiters.clear()
    return loop


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Возвращает или создаёт asyncio.Lock для конкретного device_id."""
    clean_id = device_id.strip()
    _sync_loop_state()
    lock = _device_locks.get(clean_id)
    if lock is None:
        lock = asyncio.Lock()
        _device_locks[clean_id] = lock
    return lock


def _get_pool_condition() -> asyncio.Condition:
    """Возвращает или создаёт asyncio.Condition, привязанный к текущему циклу событий."""
    global _pool_condition
    _sync_loop_state()
    if _pool_condition is None:
        _pool_condition = asyncio.Condition()
    return _pool_condition


def _reset_pool() -> None:
    """Сброс состояния пула (для изоляции тестов)."""
    global _pool_condition, _pool_loop, _rr_counter, _seq_counter, _waiters, _clock, _jobs_unsupported
    _busy_devices.clear()
    _device_locks.clear()
    _pool_condition = None
    _pool_loop = None
    _rr_counter = 0
    _seq_counter = 0
    _clock = None
    _jobs_unsupported = False
    for w in _waiters:
        if w.timer_handle is not None:
            w.timer_handle.cancel()
            w.timer_handle = None
        if not w.fut.done():
            w.fut.cancel()
    _waiters.clear()


def _get_registry(settings: Settings) -> Any | None:
    reg = getattr(settings, "phones", None)
    if reg is not None:
        return reg
    storage_dir = getattr(settings, "storage_dir", None)
    if storage_dir is not None:
        phones_path = Path(storage_dir) / "phones.json"
        if phones_path.exists():
            try:
                from .phones import PhoneRegistry

                reg = PhoneRegistry(
                    storage_dir,
                    phones_new_policy=getattr(settings, "phones_new_policy", "check"),
                    phones_snapshot_ttl_seconds=getattr(settings, "phones_snapshot_ttl_seconds", 180),
                    phones_hold=getattr(settings, "phones_hold", None),
                    phone_min_battery=getattr(settings, "phone_min_battery", 15),
                )
                return reg
            except Exception as e:
                logger.warning("Не удалось загрузить PhoneRegistry: %s", e)
    return None


def _get_phones_hold(settings: Settings) -> set[str]:
    hold_set: set[str] = set()
    env_hold = os.getenv("PHONES_HOLD")
    if env_hold:
        for x in env_hold.split(","):
            s = x.strip()
            if s:
                hold_set.add(s)
    s_hold = getattr(settings, "phones_hold", None)
    if s_hold:
        for x in s_hold:
            s = str(x).strip()
            if s:
                hold_set.add(s)
    return hold_set


def candidates(
    settings: Settings,
    site: SiteConfig,
    registry: Any | None = None,
    priority: int = 0,
) -> list[str]:
    """Возвращает список идентификаторов устройств для сайта.

    Итоговый список для сайта:
    [телефоны из site.phone в заданном порядке, которые is_usable] +
    [остальные is_usable телефоны реестра, не перечисленные в site.phone — в конце как запасные;
    порядок — по first_seen].

    Расширение применяется только для списков «a,b» и «auto» (для «auto» — все пригодные телефоны).
    Одиночный phone остаётся закреплённым за этим телефоном без расширения (при заморозке не используется).
    Поле phone_extend: false отключает добавление запасных.
    При пустом/недоступном реестре — прежнее поведение по site.phone и DROIDHUB_DEVICES.
    При включённом SPEED_ROUTING=1 порядок кандидатов переупорядочивается по реальным замерам скорости.
    """
    phone = (getattr(site, "phone", None) or "").strip()
    if not phone:
        return []

    reg = registry if registry is not None else _get_registry(settings)
    phones_hold = _get_phones_hold(settings)
    site_name = getattr(site, "name", "")
    phone_extend = getattr(site, "phone_extend", True)
    if phone_extend is None:
        phone_extend = True
    speed_routing_enabled = getattr(settings, "speed_routing", True)

    def is_usable_phone(dev_id: str) -> bool:
        if dev_id in phones_hold:
            return False
        if reg is not None and dev_id in reg:
            return bool(reg.is_usable(dev_id, site_name, ignore_battery=not speed_routing_enabled))
        # Телефоны, которых нет в реестре, но перечислены в site.phone,
        # остаются пригодными (обратная совместимость), если не заморожены.
        return True

    def get_other_usable_phones(exclude: set[str]) -> list[str]:
        if reg is None or len(reg) == 0:
            return []
        all_phones = reg.all()

        def sort_key(item: tuple[str, dict[str, Any]]) -> tuple:
            fs = item[1].get("first_seen")
            if isinstance(fs, (int, float)):
                return (0, float(fs))
            if isinstance(fs, str):
                try:
                    return (0, float(fs))
                except ValueError:
                    return (1, fs)
            return (2, str(fs or ""))

        sorted_items = sorted(all_phones.items(), key=sort_key)
        others: list[str] = []
        for dev_id, _ in sorted_items:
            clean_id = dev_id.strip()
            if (
                clean_id not in exclude
                and clean_id not in phones_hold
                and reg.is_usable(clean_id, site_name, ignore_battery=not speed_routing_enabled)
            ):
                others.append(clean_id)
        return others

    # Случай 1: auto
    if phone == "auto":
        devs = getattr(settings, "droidhub_devices", None) or []
        listed = [d.strip() for d in devs if d and d.strip()]
        usable_listed = [d for d in listed if is_usable_phone(d)]
        if phone_extend:
            others = get_other_usable_phones(set(listed))
            clean = usable_listed + others
        else:
            clean = usable_listed

        if not clean:
            return []
        if not speed_routing_enabled or len(clean) == 1:
            return clean
        try:
            from .speed import get_speed_table
            table = get_speed_table(settings)
            res = table.order_candidates(site, clean, priority=priority)
            return res
        except Exception as e:
            logger.warning("Ошибка speed_routing для auto (%s): %s", site_name, e)
            return clean

    # Случай 2: список через запятую
    if "," in phone:
        clean_site: list[str] = []
        for p in phone.split(","):
            s = p.strip()
            if s and s not in clean_site:
                clean_site.append(s)

        usable_listed = [p for p in clean_site if is_usable_phone(p)]
        if phone_extend:
            others = get_other_usable_phones(set(clean_site))
            clean = usable_listed + others
        else:
            clean = usable_listed

        patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
        if not clean:
            return []
        if not speed_routing_enabled or len(clean) == 1:
            return PreferenceList(clean, patience=patience)
        try:
            from .speed import get_speed_table
            table = get_speed_table(settings)
            res = table.order_candidates(site, clean, priority=priority)
            if isinstance(res, PreferenceList):
                return res
            return PreferenceList(res, patience=patience)
        except Exception as e:
            logger.warning("Ошибка speed_routing для списка (%s): %s", site_name, e)
            return PreferenceList(clean, patience=patience)

    # Случай 3: одиночный телефон
    if not is_usable_phone(phone):
        return []
    return [phone]


def phone_enabled(settings: Settings, site: SiteConfig) -> bool:
    """True, если заданы droidhub_url и droidhub_token, site.phone непустой и
    есть хотя бы один пригодный телефон в candidates(settings, site).
    """
    phone = (getattr(site, "phone", None) or "").strip()
    url = (getattr(settings, "droidhub_url", None) or "").strip()
    token = (getattr(settings, "droidhub_token", None) or "").strip()
    if not (phone and url and token):
        return False
    return bool(candidates(settings, site))


def _clean_candidates(candidates: list[str]) -> list[str]:
    clean: list[str] = []
    for c in candidates:
        s = c.strip()
        if s and s not in clean:
            clean.append(s)
    return clean


def _is_device_available(dev: str) -> bool:
    lock = _device_locks.get(dev)
    is_locked = lock.locked() if lock is not None else False
    return dev not in _busy_devices and not is_locked


def _try_acquire(clean: list[str], prefer_order: bool) -> str | None:
    global _rr_counter
    if prefer_order:
        for dev in clean:
            if _is_device_available(dev):
                _busy_devices.add(dev)
                return dev
        return None
    else:
        n = len(clean)
        offset = _rr_counter % n
        for i in range(n):
            dev = clean[(offset + i) % n]
            if _is_device_available(dev):
                _busy_devices.add(dev)
                _rr_counter = (_rr_counter + 1) % 1_000_000
                return dev
        return None


def _on_patience_timeout(waiter: _Waiter) -> None:
    """Коллбэк таймера терпения: будит диспетчер, когда терпение истекло."""
    waiter.timer_handle = None
    if not waiter.fut.done():
        _dispatch()


def _dispatch() -> None:
    """Проверяет очередь ожидающих и назначает свободные устройства."""
    if not _waiters:
        return
    now = _time()
    i = 0
    while i < len(_waiters):
        waiter = _waiters[i]
        if waiter.fut.done():
            if waiter.timer_handle is not None:
                waiter.timer_handle.cancel()
                waiter.timer_handle = None
            _waiters.pop(i)
            continue

        # Если действует терпение (предпочтительный порядок, терпение > 0, есть запасные,
        # и дедлайн ещё не наступил с допуском 1e-4), пробуем ТОЛЬКО предпочтительные телефоны
        pref_cnt = getattr(waiter, "preferred_count", 1) or 1
        is_patience_active = (
            waiter.prefer_order
            and waiter.patience > 0
            and len(waiter.candidates) > pref_cnt
            and (waiter.deadline - now > 1e-4)
        )
        if is_patience_active:
            cand = waiter.candidates[:pref_cnt]
            if waiter.timer_handle is None:
                loop = asyncio.get_running_loop()
                waiter.timer_handle = loop.call_later(
                    max(0.0, waiter.deadline - now), _on_patience_timeout, waiter
                )
        else:
            cand = waiter.candidates

        dev = _try_acquire(cand, waiter.prefer_order)
        if dev is not None:
            if waiter.timer_handle is not None:
                waiter.timer_handle.cancel()
                waiter.timer_handle = None
            _waiters.pop(i)
            if not waiter.fut.done():
                waiter.fut.set_result(dev)
        else:
            i += 1


async def acquire(
    candidates: list[str],
    priority: int = 0,
    weight: int = 0,
    *,
    prefer_order: bool | None = None,
    patience: float | None = None,
    patience_seconds: float | None = None,
    phone_patience_seconds: float | None = None,
    preferred_count: int | None = None,
) -> str:
    """Ожидает, пока не освободится подходящее устройство из списка кандидатов.

    Очередь ожидающих упорядочена по ключу (priority, -weight, seq):
    - priority: int (0 — человек, 1 — подписка, 2 — фон);
    - weight: int (тяжесть сайта, больше значит раньше);
    - seq: int (порядковый номер прихода).

    Для prefer_order=True устройства проверяются в порядке списка;
    для round-robin (auto) — начиная со смещения счётчика round-robin.

    Если задано терпение (patience > 0), пока предпочтительный телефон занят,
    запасные не берутся до истечения N секунд.
    """
    _sync_loop_state()
    clean = _clean_candidates(candidates)
    if not clean:
        raise ValueError("список устройств-кандидатов пуст")

    if prefer_order is None:
        prefer_order = getattr(candidates, "prefer_order", False)

    if patience is None:
        if patience_seconds is not None:
            patience = patience_seconds
        elif phone_patience_seconds is not None:
            patience = phone_patience_seconds
        else:
            patience = getattr(candidates, "patience", 0.0)
    patience_val = max(0.0, float(patience or 0.0))

    if preferred_count is None:
        preferred_count = getattr(candidates, "preferred_count", 1)
    preferred_count_val = max(1, min(int(preferred_count or 1), len(clean)))

    global _seq_counter
    _seq_counter += 1
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    now = _time()
    waiter = _Waiter(
        priority=priority,
        weight=weight,
        seq=_seq_counter,
        candidates=clean,
        prefer_order=bool(prefer_order),
        fut=fut,
        patience=patience_val,
        deadline=now + patience_val,
        preferred_count=preferred_count_val,
    )

    # Вставляем в очередь с сохранением сортировки по (priority, -weight, seq)
    keys = [w.key for w in _waiters]
    idx = bisect.bisect_right(keys, waiter.key)
    _waiters.insert(idx, waiter)

    _dispatch()

    if fut.done() and not fut.cancelled():
        return fut.result()

    if waiter.prefer_order and waiter.patience > 0 and len(waiter.candidates) > waiter.preferred_count:
        if waiter.timer_handle is None:
            rem = max(0.0, waiter.deadline - _time())
            waiter.timer_handle = loop.call_later(rem, _on_patience_timeout, waiter)

    try:
        return await fut
    except asyncio.CancelledError:
        if waiter.timer_handle is not None:
            waiter.timer_handle.cancel()
            waiter.timer_handle = None
        if fut.done() and not fut.cancelled():
            dev = fut.result()
            await release(dev)
        else:
            if waiter in _waiters:
                _waiters.remove(waiter)
        raise


async def release(device_id: str) -> None:
    """Освобождает устройство и будит ожидающие запросы в порядке приоритета."""
    clean = device_id.strip()
    _sync_loop_state()
    _busy_devices.discard(clean)
    _dispatch()
    cond = _get_pool_condition()
    async with cond:
        cond.notify_all()


def _is_retryable_error(exc: Exception) -> bool:
    """True для ошибок, при которых имеет смысл повторить запрос на другом телефоне.

    Повторяем при:
    - 502 (телефон недоступен или страница не открылась);
    - 429 (очередь занята);
    - сетевых ошибках и таймаутах.
    Ошибки авторизации (401/403) не повторяем.
    """
    if not isinstance(exc, DroidhubError):
        return False
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return False
    msg = str(exc).lower()
    if "401" in msg or "403" in msg or "авторизац" in msg:
        return False
    if status in (502, 429):
        return True
    if "502" in msg or "429" in msg or "таймаут" in msg or "сетевая ошибка" in msg or "timeout" in msg:
        return True
    return False


async def _fetch_device_snapshot(
    settings: Settings,
    device_id: str,
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[Snapshot, bool]:
    """Выполняет непосредственный запрос к сервису droidhub для конкретного устройства."""
    device_id = device_id.strip()
    lock = _get_device_lock(device_id)
    async with lock:
        wait_val = max(1, min(int(wait_seconds), 60))
        timeout = float(wait_seconds + 60)
        endpoint = f"{settings.droidhub_url.strip().rstrip('/')}/api/devices/{device_id}/fetch_page"
        headers = {"Authorization": f"Bearer {settings.droidhub_token.strip()}"}
        payload: dict[str, Any] = {
            "url": url,
            "wait_seconds": wait_val,
            "capture_json": list(capture_json) if capture_json is not None else [],
            "max_chars": 200000,
        }
        if wait_for:
            payload["wait_for"] = wait_for

        try:
            if client is not None:
                resp = await client.post(endpoint, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=timeout) as cl:
                    resp = await cl.post(endpoint, json=payload, headers=headers)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise DroidhubError("таймаут при обращении к droidhub")
        except (httpx.RequestError, ConnectionError, OSError):
            raise DroidhubError("сетевая ошибка при обращении к droidhub")

        if resp.status_code in (401, 403):
            raise DroidhubError(
                f"ошибка авторизации droidhub ({resp.status_code}): неверный токен",
                status_code=resp.status_code,
            )
        if resp.status_code == 429:
            raise DroidhubError("очередь droidhub занята (429)", status_code=429)
        if resp.status_code == 502:
            raise DroidhubError("телефон недоступен или страница не открылась (502)", status_code=502)
        if resp.status_code != 200:
            raise DroidhubError(f"ошибка droidhub (код {resp.status_code})", status_code=resp.status_code)

        try:
            data = resp.json()
        except Exception:
            raise DroidhubError("некорректный JSON в ответе droidhub")

        if not isinstance(data, dict):
            raise DroidhubError("некорректная структура ответа droidhub")

        resp_url = str(data.get("url") or url)
        resp_text = data.get("text") or ""
        if not isinstance(resp_text, str):
            raise DroidhubError("некорректный формат текста в ответе droidhub")

        captcha = bool(data.get("captcha", False))
        finished = bool(data.get("finished", True))

        raw_responses = data.get("json_responses")
        if raw_responses is None:
            raw_responses = []
        elif not isinstance(raw_responses, list):
            raise DroidhubError("некорректный формат json_responses в ответе droidhub")

        json_payloads: list[tuple[str, str]] = []
        payload_times: list[float | None] = []
        for item in raw_responses:
            if not isinstance(item, dict):
                raise DroidhubError("некорректный элемент в json_responses")
            item_url = str(item.get("url", ""))
            raw_body = item.get("body", "")
            if isinstance(raw_body, str):
                body_str = raw_body
            elif isinstance(raw_body, (dict, list)):
                body_str = json.dumps(raw_body, ensure_ascii=False)
            elif raw_body is None:
                body_str = ""
            else:
                body_str = str(raw_body)
            json_payloads.append((item_url, body_str))

            t_val = item.get("t")
            if isinstance(t_val, (int, float)) and not isinstance(t_val, bool):
                payload_times.append(float(t_val))
            else:
                payload_times.append(None)

        woke_val = data.get("woke")
        if isinstance(woke_val, bool):
            woke: bool | None = woke_val
        elif isinstance(woke_val, int):
            woke = bool(woke_val)
        else:
            woke = None

        tfm_val = data.get("t_first_match")
        if isinstance(tfm_val, (int, float)) and not isinstance(tfm_val, bool):
            t_first_match: float | None = float(tfm_val)
        else:
            t_first_match = None

        snap = Snapshot(
            url=resp_url,
            text=resp_text,
            json_payloads=json_payloads,
            blocked=captcha,
            payload_times=payload_times,
            woke=woke,
            t_first_match=t_first_match,
        )
        return snap, finished


async def _delete_job(
    delete_url: str,
    headers: dict[str, str],
    client: httpx.AsyncClient | None = None,
) -> None:
    """Удаляет/отменяет задание в droidhub."""
    try:
        if client is not None:
            await client.delete(delete_url, headers=headers, timeout=DELETE_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=DELETE_TIMEOUT) as cl:
                await cl.delete(delete_url, headers=headers)
    except Exception as e:
        logger.warning("Ошибка при вызове DELETE %s: %s", delete_url, e)


async def _fetch_device_job(
    settings: Settings,
    device_id: str,
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    evaluate: Callable[[Snapshot, dict[str, Any]], Awaitable[str | None]] | None = None,
    client: httpx.AsyncClient | None = None,
    max_seconds: float | None = None,
) -> tuple[Snapshot, bool, dict[str, Any]]:
    """Выполняет запрос через механизм заданий fetch_jobs в droidhub."""
    device_id = device_id.strip()
    lock = _get_device_lock(device_id)
    async with lock:
        wait_val = max(1, min(int(wait_seconds), 60))
        max_sec = 45.0 if max_seconds is None else float(max_seconds)
        max_sec = max(1.0, min(max_sec, 90.0))
        base_url = settings.droidhub_url.strip().rstrip("/")
        token = settings.droidhub_token.strip()
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = f"{base_url}/api/devices/{device_id}/fetch_jobs"
        payload: dict[str, Any] = {
            "url": url,
            "wait_seconds": wait_val,
            "max_seconds": max_sec,
            "capture_json": list(capture_json) if capture_json is not None else [],
            "max_chars": 200000,
        }
        if wait_for:
            payload["wait_for"] = wait_for

        timeout = float(wait_seconds + 60)
        job_id: str | None = None
        job_url: str = ""

        try:
            try:
                if client is not None:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=timeout) as cl:
                        resp = await cl.post(endpoint, json=payload, headers=headers)
            except (httpx.TimeoutException, asyncio.TimeoutError):
                raise DroidhubError("таймаут при обращении к droidhub")
            except (httpx.RequestError, ConnectionError, OSError):
                raise DroidhubError("сетевая ошибка при обращении к droidhub")

            if resp.status_code in (401, 403):
                raise DroidhubError(
                    f"ошибка авторизации droidhub ({resp.status_code}): неверный токен",
                    status_code=resp.status_code,
                )
            if resp.status_code in (404, 405):
                raise JobsUnsupported(
                    f"хаб не поддерживает fetch_jobs ({resp.status_code})",
                    status_code=resp.status_code,
                )
            if resp.status_code == 429:
                raise DroidhubError("очередь droidhub занята (429)", status_code=429)
            if resp.status_code == 502:
                raise DroidhubError("телефон недоступен или страница не открылась (502)", status_code=502)
            if resp.status_code != 200:
                raise DroidhubError(f"ошибка droidhub (код {resp.status_code})", status_code=resp.status_code)

            try:
                init_data = resp.json()
            except Exception:
                raise DroidhubError("некорректный JSON в ответе droidhub")
            if not isinstance(init_data, dict) or "job_id" not in init_data:
                raise DroidhubError("некорректная структура ответа droidhub: отсутствует job_id")

            job_id = str(init_data["job_id"]).strip()
            job_url = f"{base_url}/api/fetch_jobs/{job_id}"
            finish_url = f"{job_url}/finish"

            accumulated_payloads: list[tuple[str, str]] = []
            accumulated_times: list[float | None] = []
            next_since = 0
            stop_reason: str | None = None
            hub_data: dict[str, Any] = {}
            t_start = time.monotonic()

            while True:
                t_poll_start = time.monotonic()
                poll_url = f"{job_url}?since={next_since}"
                try:
                    if client is not None:
                        poll_resp = await client.get(poll_url, headers=headers)
                    else:
                        async with httpx.AsyncClient(timeout=10.0) as cl:
                            poll_resp = await cl.get(poll_url, headers=headers)
                except (httpx.TimeoutException, asyncio.TimeoutError):
                    raise DroidhubError("таймаут при обращении к droidhub")
                except (httpx.RequestError, ConnectionError, OSError):
                    raise DroidhubError("сетевая ошибка при обращении к droidhub")

                if poll_resp.status_code in (401, 403):
                    raise DroidhubError(
                        f"ошибка авторизации droidhub ({poll_resp.status_code}): неверный токен",
                        status_code=poll_resp.status_code,
                    )
                if poll_resp.status_code == 502:
                    raise DroidhubError("телефон недоступен или страница не открылась (502)", status_code=502)
                if poll_resp.status_code != 200:
                    raise DroidhubError(f"ошибка droidhub (код {poll_resp.status_code})", status_code=poll_resp.status_code)

                try:
                    hub_data = poll_resp.json()
                except Exception:
                    raise DroidhubError("некорректный JSON в ответе droidhub")
                if not isinstance(hub_data, dict):
                    raise DroidhubError("некорректная структура ответа droidhub")

                raw_responses = hub_data.get("responses") or []
                next_since = hub_data.get("next", next_since)
                for item in raw_responses:
                    if isinstance(item, dict):
                        item_url = str(item.get("url", ""))
                        raw_body = item.get("body", "")
                        if isinstance(raw_body, str):
                            body_str = raw_body
                        elif isinstance(raw_body, (dict, list)):
                            body_str = json.dumps(raw_body, ensure_ascii=False)
                        elif raw_body is None:
                            body_str = ""
                        else:
                            body_str = str(raw_body)
                        accumulated_payloads.append((item_url, body_str))
                        t_val = item.get("t")
                        if isinstance(t_val, (int, float)) and not isinstance(t_val, bool):
                            accumulated_times.append(float(t_val))
                        else:
                            accumulated_times.append(None)

                snap_so_far = Snapshot(
                    url=url,
                    text="",
                    json_payloads=list(accumulated_payloads),
                    payload_times=list(accumulated_times),
                    blocked=bool(hub_data.get("captcha", False)),
                )

                if evaluate is not None:
                    res = evaluate(snap_so_far, hub_data)
                    if inspect.isawaitable(res):
                        stop_reason = await res
                    else:
                        stop_reason = res

                job_st = hub_data.get("state")
                if stop_reason is not None:
                    break
                if job_st in ("done", "failed"):
                    stop_reason = "job_done"
                    break

                poll_elapsed = time.monotonic() - t_poll_start
                if poll_elapsed < POLL_MIN_INTERVAL:
                    await asyncio.sleep(POLL_MIN_INTERVAL - poll_elapsed)

            # Вызвать finish
            try:
                if client is not None:
                    finish_resp = await client.post(finish_url, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=10.0) as cl:
                        finish_resp = await cl.post(finish_url, headers=headers)
            except (httpx.TimeoutException, asyncio.TimeoutError):
                raise DroidhubError("таймаут при обращении к droidhub (finish)")
            except (httpx.RequestError, ConnectionError, OSError):
                raise DroidhubError("сетевая ошибка при обращении к droidhub (finish)")

            if finish_resp.status_code != 200:
                raise DroidhubError(
                    f"ошибка finish в droidhub (код {finish_resp.status_code})",
                    status_code=finish_resp.status_code,
                )

            try:
                finish_data = finish_resp.json()
            except Exception:
                raise DroidhubError("некорректный JSON в ответе finish droidhub")
            if not isinstance(finish_data, dict):
                raise DroidhubError("некорректная структура ответа finish droidhub")

            resp_url = str(finish_data.get("url") or url)
            resp_text = finish_data.get("text") or ""
            captcha = bool(finish_data.get("captcha", False) or hub_data.get("captcha", False))

            woke_val = finish_data.get("woke")
            if isinstance(woke_val, bool):
                woke: bool | None = woke_val
            elif isinstance(woke_val, int):
                woke = bool(woke_val)
            else:
                woke = None

            tfm_val = finish_data.get("t_first_match")
            if isinstance(tfm_val, (int, float)) and not isinstance(tfm_val, bool):
                t_first_match: float | None = float(tfm_val)
            else:
                t_first_match = None

            snap = Snapshot(
                url=resp_url,
                text=resp_text,
                json_payloads=accumulated_payloads,
                payload_times=accumulated_times,
                blocked=captcha,
                woke=woke,
                t_first_match=t_first_match,
            )

            finished = bool(
                stop_reason in ("done", "job_done")
                or (stop_reason not in ("stable", "quiet", "cap") and bool(hub_data.get("wait_for_seen", False)))
            )
            elapsed = float(finish_data.get("seconds") or hub_data.get("elapsed") or (time.monotonic() - t_start))

            t_first_offer = getattr(evaluate, "t_first_offer", None)
            t_last_improve = getattr(evaluate, "t_last_improve", None)

            info = {
                "stop_reason": stop_reason,
                "elapsed": elapsed,
                "t_first_offer": t_first_offer,
                "t_last_improve": t_last_improve,
            }
            return snap, finished, info

        except BaseException:
            if job_id is not None and job_url:
                try:
                    await asyncio.shield(_delete_job(job_url, headers, client))
                except Exception as del_err:
                    logger.warning("Ошибка отмены задания %s: %s", job_id, del_err)
            raise


def _infer_site_name(url: str) -> str:
    url_l = str(url).lower()
    for s in ("aviasales", "yandex", "tutu", "kupibilet", "ozon"):
        if s in url_l:
            return s
    return "site"


def _record_failure(
    settings: Settings,
    site: str,
    device: str,
    priority: int,
    error: str,
    *,
    captcha: bool = False,
    partial: bool = False,
    offers: int = 0,
    fetch_seconds: float | None = None,
    queue_wait: float | None = None,
) -> None:
    try:
        from .metrics import MetricsStore
        metrics = getattr(settings, "metrics", None)
        if metrics is None:
            storage_dir = getattr(settings, "storage_dir", None)
            if storage_dir is not None:
                metrics_path = Path(storage_dir) / "metrics.sqlite"
                if metrics_path.exists():
                    metrics = MetricsStore(metrics_path)
        if metrics is not None:
            source = "user" if priority == 0 else ("sub" if priority == 1 else "bg")
            is_ok = 1 if (partial and offers > 0) else 0
            metrics.record_run(
                source=source,
                site=site,
                ok=is_ok,
                partial=partial,
                captcha=captcha,
                error=error,
                offers=offers,
                elapsed=fetch_seconds or 0.0,
                device=device,
                queue_wait=queue_wait,
                fetch_seconds=fetch_seconds,
                priority=priority,
            )
            try:
                from .speed import get_speed_table
                table = get_speed_table(settings)
                table.invalidate()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Не удалось записать провал в метрики (%s): %s", device, e)


async def fetch_page_snapshot_any(
    settings: Settings,
    candidates: list[str],
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    client: httpx.AsyncClient | None = None,
    priority: int = 0,
    weight: int = 0,
    stats: dict | None = None,
    prefer_order: bool | None = None,
    patience: float | None = None,
    patience_seconds: float | None = None,
    phone_patience_seconds: float | None = None,
    site_name: str | None = None,
    preferred_count: int | None = None,
    evaluate: Callable[[Snapshot, dict[str, Any]], Awaitable[str | None]] | None = None,
    max_seconds: float | None = None,
) -> tuple[Snapshot, bool]:
    """Открывает страницу на любом свободном телефоне из candidates.

    Берёт свободное устройство через acquire, делает запрос. Если телефон
    недоступен (502), очередь занята (429) или возникла сетевая ошибка/таймаут,
    и есть другие непробованные кандидаты — повторяет на следующем свободном устройстве.
    Каждое устройство опрашивается максимум один раз за вызов.
    Ошибки авторизации (401/403) пробрасываются сразу.
    При капче на устройстве запрос повторяется на следующем кандидате.
    """
    clean_candidates = _clean_candidates(candidates)
    if not clean_candidates:
        raise DroidhubError("список устройств пуст")

    if prefer_order is None:
        prefer_order = getattr(candidates, "prefer_order", False)

    if preferred_count is None:
        preferred_count = getattr(candidates, "preferred_count", 1)

    if patience is None:
        if patience_seconds is not None:
            patience = patience_seconds
        elif phone_patience_seconds is not None:
            patience = phone_patience_seconds
        else:
            patience = getattr(candidates, "patience", 0.0)
    patience_val = max(0.0, float(patience or 0.0))

    tried: set[str] = set()
    last_exc: Exception | None = None
    total_queue_wait: float = 0.0
    attempts: int = 0

    pref_base = preferred_count or 1
    pref_set = set(clean_candidates[:pref_base])

    global _jobs_unsupported

    while len(tried) < len(clean_candidates):
        remaining = [c for c in clean_candidates if c not in tried]
        if not remaining:
            break

        rem_pref = sum(1 for c in remaining if c in pref_set)
        acq_patience = patience_val if rem_pref > 0 else 0.0
        acq_pref = max(1, rem_pref) if rem_pref > 0 else len(remaining)

        t_acq_start = time.monotonic()
        dev = await acquire(
            remaining,
            priority=priority,
            weight=weight,
            prefer_order=prefer_order,
            patience=acq_patience,
            preferred_count=acq_pref,
        )
        t_acq = time.monotonic() - t_acq_start
        total_queue_wait += t_acq
        attempts += 1
        tried.add(dev)

        if stats is not None:
            stats["device"] = dev
            stats["queue_wait"] = total_queue_wait
            stats["attempts"] = attempts
            stats["tried"] = list(tried)

        t_fetch_start = time.monotonic()
        try:
            use_jobs = bool(
                evaluate is not None
                and getattr(settings, "fast_stop", True)
                and not _jobs_unsupported
                and priority == 0
                and _jobs_bad_sites.get(site_name or _infer_site_name(url), 0.0) <= time.monotonic()
            )
            if use_jobs:
                try:
                    snap, finished, info = await _fetch_device_job(
                        settings,
                        dev,
                        url,
                        wait_seconds=wait_seconds,
                        capture_json=capture_json,
                        wait_for=wait_for,
                        evaluate=evaluate,
                        client=client,
                        max_seconds=max_seconds,
                    )
                    if (
                        info.get("stop_reason") == "job_done"
                        and not snap.json_payloads
                        and capture_json
                        and not snap.blocked
                    ):
                        # задание закончилось без единого ответа: хаб в этом режиме не видит выдачу сайта.
                        # Старый fetch_page её видит, поэтому берём его сразу и на время отключаем задания для сайта.
                        bad_name = site_name or _infer_site_name(url)
                        _jobs_bad_sites[bad_name] = time.monotonic() + JOBS_BAD_SITE_SECONDS
                        logger.warning("Задание хаба для %s не отдало ответов, переходим на fetch_page на %d с", bad_name, int(JOBS_BAD_SITE_SECONDS))
                        snap, finished = await _fetch_device_snapshot(
                            settings,
                            dev,
                            url,
                            wait_seconds=wait_seconds,
                            capture_json=capture_json,
                            wait_for=wait_for,
                            client=client,
                        )
                        fetch_sec = time.monotonic() - t_fetch_start
                        if stats is not None:
                            stats["fetch_seconds"] = fetch_sec
                            stats["tried"] = list(tried)
                        return snap, finished
                    fetch_sec = time.monotonic() - t_fetch_start
                    if stats is not None:
                        stats["fetch_seconds"] = fetch_sec
                        stats["tried"] = list(tried)
                        stats["stop_reason"] = info.get("stop_reason")
                        stats["t_first_offer"] = info.get("t_first_offer")
                        stats["t_last_improve"] = info.get("t_last_improve")
                        stats["job_elapsed"] = info.get("elapsed")
                    return snap, finished
                except JobsUnsupported:
                    _jobs_unsupported = True
                    snap, finished = await _fetch_device_snapshot(
                        settings,
                        dev,
                        url,
                        wait_seconds=wait_seconds,
                        capture_json=capture_json,
                        wait_for=wait_for,
                        client=client,
                    )
                    fetch_sec = time.monotonic() - t_fetch_start
                    if stats is not None:
                        stats["fetch_seconds"] = fetch_sec
                        stats["tried"] = list(tried)
                    return snap, finished
            else:
                snap, finished = await _fetch_device_snapshot(
                    settings,
                    dev,
                    url,
                    wait_seconds=wait_seconds,
                    capture_json=capture_json,
                    wait_for=wait_for,
                    client=client,
                )
                fetch_sec = time.monotonic() - t_fetch_start
                if stats is not None:
                    stats["fetch_seconds"] = fetch_sec
                    stats["tried"] = list(tried)
                return snap, finished
        except Exception as exc:
            fetch_sec = time.monotonic() - t_fetch_start
            if stats is not None:
                stats["fetch_seconds"] = fetch_sec
                stats["tried"] = list(tried)
            last_exc = exc
            eff_site = site_name or _infer_site_name(url)
            if any(c for c in clean_candidates if c not in tried) and _is_retryable_error(exc):
                _record_failure(
                    settings=settings,
                    site=eff_site,
                    device=dev,
                    priority=priority,
                    error=str(exc),
                    captcha=False,
                    fetch_seconds=fetch_sec,
                    queue_wait=t_acq,
                )
            if not _is_retryable_error(exc):
                raise
            if not any(c for c in clean_candidates if c not in tried):
                raise
            log_exc = str(exc)
            token = (getattr(settings, "droidhub_token", "") or "").strip()
            if token and token in log_exc:
                log_exc = log_exc.replace(token, "[REDACTED]")
            logger.warning("Устройство %s вернуло ошибку (%s), пробуем следующее устройство", dev, log_exc)
        finally:
            await release(dev)

    if last_exc is not None:
        raise last_exc
    raise DroidhubError("не удалось получить снимок страницы ни с одного устройства")


async def fetch_page_snapshot(
    settings: Settings,
    device_id: str | list[str] | PreferenceList,
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    client: httpx.AsyncClient | None = None,
    priority: int = 0,
    weight: int = 0,
    stats: dict | None = None,
    prefer_order: bool | None = None,
    patience: float | None = None,
    patience_seconds: float | None = None,
    phone_patience_seconds: float | None = None,
    site_name: str | None = None,
    preferred_count: int | None = None,
    evaluate: Callable[[Snapshot, dict[str, Any]], Awaitable[str | None]] | None = None,
    max_seconds: float | None = None,
) -> tuple[Snapshot, bool]:
    """Открывает страницу на телефоне через droidhub и возвращает (Snapshot, finished).

    Сохраняет совместимость с прежней сигнатурой (device_id: str) и поддерживает
    передачу списка кандидатов (device_id: list[str] или PreferenceList).
    """
    if prefer_order is None:
        prefer_order = getattr(device_id, "prefer_order", None)

    if preferred_count is None:
        preferred_count = getattr(device_id, "preferred_count", None)

    if patience is None:
        if patience_seconds is not None:
            patience = patience_seconds
        elif phone_patience_seconds is not None:
            patience = phone_patience_seconds
        else:
            patience = getattr(device_id, "patience", None)

    if isinstance(device_id, (list, tuple)):
        devs = device_id if isinstance(device_id, list) else list(device_id)
        return await fetch_page_snapshot_any(
            settings,
            devs,
            url,
            wait_seconds=wait_seconds,
            capture_json=capture_json,
            wait_for=wait_for,
            client=client,
            priority=priority,
            weight=weight,
            stats=stats,
            prefer_order=prefer_order,
            patience=patience,
            site_name=site_name,
            preferred_count=preferred_count,
            evaluate=evaluate,
            max_seconds=max_seconds,
        )
    return await fetch_page_snapshot_any(
        settings,
        [device_id],
        url,
        wait_seconds=wait_seconds,
        capture_json=capture_json,
        wait_for=wait_for,
        client=client,
        priority=priority,
        weight=weight,
        stats=stats,
        prefer_order=prefer_order,
        patience=patience,
        site_name=site_name,
        preferred_count=preferred_count,
        evaluate=evaluate,
        max_seconds=max_seconds,
    )
