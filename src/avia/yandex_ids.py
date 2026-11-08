"""Разрешение IATA-кодов городов и аэропортов во внутренние идентификаторы Яндекс Путешествий."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

import httpx

SUGGEST_URL = "https://suggests.avia.yandex.ru/v2/avia"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT_SECONDS = 10.0
NEGATIVE_CACHE_TTL = 600.0  # 10 минут

# Статичная проверенная таблица IATA -> id Яндекс Путешествий
STATIC_CITIES: dict[str, str] = {
    "MOW": "c213",
    "LED": "c2",
    "KUF": "c51",
    "SVX": "c54",
    "KZN": "c43",
    "AER": "c239",
    "OVB": "c65",
    "UFA": "c172",
    "MSQ": "c157",
    "ALA": "c22177",
    "NQZ": "c163",
    "TAS": "c10335",
    "SKD": "c10334",
    "FRU": "c10309",
    "EVN": "c10262",
    "GYD": "c10253",
    "TBS": "c10277",
    "DYU": "c10318",
    "IST": "c11508",
    "DXB": "c11499",
}

# Кэш в памяти процесса: {IATA: point_key}
_MEM_CACHE: dict[str, str] = {}

# Отрицательный кэш в памяти: {IATA: expires_at_monotonic}
_NEG_CACHE: dict[str, float] = {}

# Блокировки для параллельных запросов одного кода
_LOCKS: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    """Сбрасывает кэши в памяти."""
    _MEM_CACHE.clear()
    _NEG_CACHE.clear()
    _LOCKS.clear()


def get_cache_path() -> Path:
    """Возвращает путь к файловому кэшу идентификаторов."""
    env_path = os.environ.get("YANDEX_IDS_CACHE")
    if env_path:
        return Path(env_path)
    try:
        from .config import load_settings
        settings = load_settings()
        storage_dir = getattr(settings, "storage_dir", None)
        if storage_dir is not None:
            return Path(storage_dir) / "yandex_ids_cache.json"
    except Exception:
        pass
    return Path("state/yandex_ids_cache.json")


def _read_disk_cache() -> dict[str, str]:
    """Считывает кэш из JSON-файла на диске."""
    path = get_cache_path()
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            return {str(k).strip().upper(): str(v).strip() for k, v in data.items() if v}
    except Exception:
        pass
    return {}


def _write_disk_cache_entry(code: str, point_key: str) -> None:
    """Атомарно записывает идентификатор в файловый кэш через временный файл."""
    path = get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_disk_cache()
        data[code.strip().upper()] = point_key
        tmp_path = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _get_lock(code: str) -> asyncio.Lock:
    """Возвращает asyncio.Lock для указанного кода."""
    lock = _LOCKS.get(code)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[code] = lock
    return lock


def find_point_key(data: Any, iata: str) -> str | None:
    """Ищет point_key для iata-кода среди городов и аэропортов в ответе подсказок Яндекса."""
    target = iata.strip().upper()
    if not isinstance(data, (list, tuple)) or len(data) < 2:
        return None
    items = data[1]
    if not isinstance(items, (list, tuple)):
        return None

    # Сначала проверяем верхний уровень (города или отдельные аэропорты)
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) > 2 and isinstance(item[2], dict):
            code = str(item[2].get("point_code") or "").strip().upper()
            if code == target:
                point_key = item[2].get("point_key")
                if point_key:
                    return str(point_key)

    # Затем проверяем вложенные аэропорты
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) > 3 and isinstance(item[3], (list, tuple)):
            for child in item[3]:
                if isinstance(child, (list, tuple)) and len(child) > 2 and isinstance(child[2], dict):
                    code = str(child[2].get("point_code") or "").strip().upper()
                    if code == target:
                        point_key = child[2].get("point_key")
                        if point_key:
                            return str(point_key)

    return None


def _build_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(**kwargs)


async def _fetch_from_suggest(code: str, client: httpx.AsyncClient | None = None, orig_iata: str = "") -> str:
    """Выполняет запрос к подсказкам Яндекса с таймаутом 10 с и 1 повтором при сетевой ошибке."""
    params = {
        "query": code,
        "lang": "ru",
        "field": "to",
    }
    headers = {"User-Agent": DEFAULT_USER_AGENT}

    resp: httpx.Response | None = None
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            if client is not None:
                resp = await client.get(
                    SUGGEST_URL,
                    params=params,
                    headers=headers,
                    timeout=TIMEOUT_SECONDS,
                )
            else:
                async with _build_client(timeout=TIMEOUT_SECONDS, headers=headers) as c:
                    resp = await c.get(
                        SUGGEST_URL,
                        params=params,
                    )
            break
        except (httpx.RequestError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == 0:
                continue
            raise RuntimeError(
                f"Ошибка сети при запросе подсказок Яндекса для {code}: {exc}"
            ) from exc

    if resp is None:
        raise RuntimeError(
            f"Ошибка сети при запросе подсказок Яндекса для {code}: {last_exc}"
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Ошибка сервиса подсказок Яндекса для {code} (статус {resp.status_code})")

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Некорректный JSON в ответе подсказчика Яндекса для {code}: {exc}") from exc

    point_key = find_point_key(data, code)
    if not point_key:
        _NEG_CACHE[code] = time.monotonic() + NEGATIVE_CACHE_TTL
        raise RuntimeError(
            f"Яндекс Путешествия: не нашёл id для {orig_iata or code}"
        )

    return point_key


async def resolve(iata: str, client: httpx.AsyncClient | None = None) -> str:
    """Асинхронно разрешает IATA-код города или аэропорта во внутренний идентификатор Яндекс Путешествий."""
    code = iata.strip().upper()
    if not code:
        raise RuntimeError("Яндекс Путешествия: не указан IATA-код")

    # 1) STATIC_CITIES (без сети)
    if code in STATIC_CITIES:
        return STATIC_CITIES[code]

    # 2) Кэш в памяти процесса
    if code in _MEM_CACHE:
        return _MEM_CACHE[code]

    # Отрицательный кэш в памяти (10 минут)
    now = time.monotonic()
    if code in _NEG_CACHE:
        if now < _NEG_CACHE[code]:
            raise RuntimeError(
                f"Яндекс Путешествия: не нашёл id для {iata}"
            )
        _NEG_CACHE.pop(code, None)

    # 3) Файловый кэш (диск)
    disk_data = _read_disk_cache()
    if code in disk_data:
        val = disk_data[code]
        _MEM_CACHE[code] = val
        return val

    # 4) Запрос к эндпоинту подсказок с защитой от параллельных запросов
    async with _get_lock(code):
        if code in _MEM_CACHE:
            return _MEM_CACHE[code]

        now = time.monotonic()
        if code in _NEG_CACHE:
            if now < _NEG_CACHE[code]:
                raise RuntimeError(
                    f"Яндекс Путешествия: не нашёл id для {iata}"
                )
            _NEG_CACHE.pop(code, None)

        disk_data = _read_disk_cache()
        if code in disk_data:
            val = disk_data[code]
            _MEM_CACHE[code] = val
            return val

        point_key = await _fetch_from_suggest(code, client=client, orig_iata=iata)
        _MEM_CACHE[code] = point_key
        _write_disk_cache_entry(code, point_key)
        return point_key


def city_id(iata: str) -> str:
    """Возвращает внутренний идентификатор Яндекс Путешествий (например, 'c213') для IATA-кода."""
    code = iata.strip().upper()
    if code in STATIC_CITIES:
        return STATIC_CITIES[code]
    if code in _MEM_CACHE:
        return _MEM_CACHE[code]
    raise RuntimeError(
        f"Яндекс Путешествия: город {iata} пока не поддерживается (нет id в таблице yandex_ids.py)"
    )
