"""Интеграция с сервисом droidhub для сбора страниц через Android-телефон."""
from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable

import httpx

from .collector import Snapshot
from .config import Settings, SiteConfig

logger = logging.getLogger(__name__)


class PreferenceList(list):
    """Список устройств с фиксированным порядком предпочтения (не round-robin)."""
    prefer_order: bool = True
    patience: float = 0.0

    def __init__(self, iterable: Any = (), *, patience: float | None = None) -> None:
        super().__init__(iterable)
        if patience is not None:
            self.patience = float(patience)


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
    global _pool_condition, _pool_loop, _rr_counter, _seq_counter, _waiters, _clock
    _busy_devices.clear()
    _device_locks.clear()
    _pool_condition = None
    _pool_loop = None
    _rr_counter = 0
    _seq_counter = 0
    _clock = None
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
                    phones_new_policy=getattr(settings, "phones_new_policy", "active"),
                    phones_snapshot_ttl_seconds=getattr(settings, "phones_snapshot_ttl_seconds", 180),
                    phones_hold=getattr(settings, "phones_hold", None),
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

    def is_usable_phone(dev_id: str) -> bool:
        if dev_id in phones_hold:
            return False
        if reg is not None and dev_id in reg:
            return bool(reg.is_usable(dev_id, site_name))
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
                and reg.is_usable(clean_id, site_name)
            ):
                others.append(clean_id)
        return others

    # Случай 1: auto
    if phone == "auto":
        devs = getattr(settings, "droidhub_devices", None) or []
        listed = [d.strip() for d in devs if d and d.strip()]
        usable_listed = [d for d in listed if is_usable_phone(d)]
        if phone_extend:
            others = get_other_usable_phones(set(usable_listed))
            return usable_listed + others
        return usable_listed

    # Случай 2: список через запятую
    if "," in phone:
        clean_site: list[str] = []
        for p in phone.split(","):
            s = p.strip()
            if s and s not in clean_site:
                clean_site.append(s)

        usable_listed = [p for p in clean_site if is_usable_phone(p)]
        if phone_extend:
            others = get_other_usable_phones(set(usable_listed))
            clean = usable_listed + others
        else:
            clean = usable_listed

        patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
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
        # и дедлайн ещё не наступил с допуском 1e-4), пробуем ТОЛЬКО предпочтительный телефон
        is_patience_active = (
            waiter.prefer_order
            and waiter.patience > 0
            and len(waiter.candidates) > 1
            and (waiter.deadline - now > 1e-4)
        )
        if is_patience_active:
            cand = waiter.candidates[:1]
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
    )

    # Вставляем в очередь с сохранением сортировки по (priority, -weight, seq)
    keys = [w.key for w in _waiters]
    idx = bisect.bisect_right(keys, waiter.key)
    _waiters.insert(idx, waiter)

    _dispatch()

    if fut.done() and not fut.cancelled():
        return fut.result()

    if waiter.prefer_order and waiter.patience > 0 and len(waiter.candidates) > 1:
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

        snap = Snapshot(
            url=resp_url,
            text=resp_text,
            json_payloads=json_payloads,
            blocked=captcha,
        )
        return snap, finished


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
) -> tuple[Snapshot, bool]:
    """Открывает страницу на любом свободном телефоне из candidates.

    Берёт свободное устройство через acquire, делает запрос. Если телефон
    недоступен (502), очередь занята (429) или возникла сетевая ошибка/таймаут,
    и есть другие непробованные кандидаты — повторяет на следующем свободном устройстве.
    Каждое устройство опрашивается максимум один раз за вызов.
    Ошибки авторизации (401/403) пробрасываются сразу.
    Капча не считается исключением и не повторяется.
    """
    clean_candidates = _clean_candidates(candidates)
    if not clean_candidates:
        raise DroidhubError("список устройств пуст")

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

    tried: set[str] = set()
    last_exc: Exception | None = None
    total_queue_wait: float = 0.0
    attempts: int = 0

    while len(tried) < len(clean_candidates):
        remaining = [c for c in clean_candidates if c not in tried]
        if not remaining:
            break

        t_acq_start = time.monotonic()
        dev = await acquire(
            remaining,
            priority=priority,
            weight=weight,
            prefer_order=prefer_order,
            patience=patience_val,
        )
        t_acq = time.monotonic() - t_acq_start
        total_queue_wait += t_acq
        attempts += 1
        tried.add(dev)

        if stats is not None:
            stats["device"] = dev
            stats["queue_wait"] = total_queue_wait
            stats["attempts"] = attempts

        t_fetch_start = time.monotonic()
        try:
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
            return snap, finished
        except Exception as exc:
            fetch_sec = time.monotonic() - t_fetch_start
            if stats is not None:
                stats["fetch_seconds"] = fetch_sec
            last_exc = exc
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
) -> tuple[Snapshot, bool]:
    """Открывает страницу на телефоне через droidhub и возвращает (Snapshot, finished).

    Сохраняет совместимость с прежней сигнатурой (device_id: str) и поддерживает
    передачу списка кандидатов (device_id: list[str] или PreferenceList).
    """
    if prefer_order is None:
        prefer_order = getattr(device_id, "prefer_order", None)

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
    )
