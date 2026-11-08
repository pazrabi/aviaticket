"""Тесты динамического разрешения идентификаторов Яндекс Путешествий (yandex_ids.py)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from avia.yandex_ids import (
    STATIC_CITIES,
    _MEM_CACHE,
    _NEG_CACHE,
    clear_cache,
    city_id,
    resolve,
)


@pytest.fixture(autouse=True)
def reset_yandex_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "test_yandex_ids_cache.json"
    monkeypatch.setenv("YANDEX_IDS_CACHE", str(cache_path))
    clear_cache()
    yield
    clear_cache()


@pytest.mark.asyncio
async def test_static_cities_no_network():
    """Статичные города возвращаются без сетевых запросов."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await resolve("MOW", client=client) == "c213"
        assert await resolve("led", client=client) == "c2"
        assert await resolve("ALA", client=client) == "c22177"

    assert not called


@pytest.mark.asyncio
async def test_dynamic_city_found_by_point_code():
    """Динамический город находится по point_code в подсказках."""
    payload = [
        "rom",
        [
            [
                0,
                "Рим",
                {
                    "point_key": "c10445",
                    "point_code": "ROM",
                    "city_title": "Рим",
                    "region_id": 21909,
                    "geo_id": 10445,
                },
                [
                    [1, "Чампино", {"point_key": "s9624403", "point_code": "CIA"}, []],
                    [1, "Фьюмичино", {"point_key": "s9600408", "point_code": "FCO"}, []],
                ],
            ]
        ],
    ]

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        res = await resolve("ROM", client=client)
        assert res == "c10445"
        # city_id теперь тоже возвращает этот город из кэша памяти
        assert city_id("ROM") == "c10445"


@pytest.mark.asyncio
async def test_airport_returns_s_key():
    """Аэропорт возвращает s-ключ как на верхнем уровне, так и во вложенном списке."""
    # 1) Вложенный аэропорт
    payload_nested = [
        "rom",
        [
            [
                0,
                "Рим",
                {"point_key": "c10445", "point_code": "ROM"},
                [
                    [1, "Чампино", {"point_key": "s9624403", "point_code": "CIA"}, []],
                    [1, "Фьюмичино", {"point_key": "s9600408", "point_code": "FCO"}, []],
                ],
            ]
        ],
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload_nested))
    async with httpx.AsyncClient(transport=transport) as client:
        res_fco = await resolve("fco", client=client)
        assert res_fco == "s9600408"
        assert res_fco.startswith("s")

    clear_cache()

    # 2) Отдельный аэропорт на верхнем уровне
    payload_standalone = [
        "vko",
        [
            [
                1,
                "Внуково",
                {"point_key": "s9600215", "point_code": "VKO", "city_title": "Москва"},
                [],
            ]
        ],
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload_standalone))
    async with httpx.AsyncClient(transport=transport) as client:
        res_vko = await resolve("VKO", client=client)
        assert res_vko == "s9600215"
        assert res_vko.startswith("s")


@pytest.mark.asyncio
async def test_repeated_call_uses_cache():
    """Повторный вызов берёт значение из кэша без нового обращения к сети."""
    payload = [
        "uus",
        [
            [0, "Южно-Сахалинск", {"point_key": "c23242", "point_code": "UUS"}, []],
        ],
    ]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res1 = await resolve("UUS", client=client)
        assert res1 == "c23242"
        assert call_count == 1

        res2 = await resolve("UUS", client=client)
        assert res2 == "c23242"
        assert call_count == 1


@pytest.mark.asyncio
async def test_network_error_retries_and_raises_runtime_error():
    """При сетевой ошибке делается 1 повтор, затем выбрасывается RuntimeError."""
    call_count = 0

    def handler_fail(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("Network unreachable")

    transport = httpx.MockTransport(handler_fail)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="Ошибка сети"):
            await resolve("ERR", client=client)

    # 1 первоначальная попытка + 1 повтор = 2 вызова
    assert call_count == 2


@pytest.mark.asyncio
async def test_network_error_retry_success():
    """Если первая попытка завершилась сетевой ошибкой, а повтор успешен — resolve возвращает результат."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Temporary glitch")
        return httpx.Response(
            200,
            json=["uus", [[0, "Южно-Сахалинск", {"point_key": "c23242", "point_code": "UUS"}, []]]],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await resolve("UUS", client=client)
        assert res == "c23242"
        assert call_count == 2


@pytest.mark.asyncio
async def test_empty_response_raises_runtime_error():
    """Пустой ответ подсказок Яндекса вызывает RuntimeError."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=["empty", []]))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match=r"Яндекс Путешествия: не нашёл id для EMPTY"):
            await resolve("EMPTY", client=client)


@pytest.mark.asyncio
async def test_not_found_code_raises_runtime_error():
    """Ответ без искомого point_code вызывает RuntimeError."""
    payload = [
        "other",
        [[0, "Другой город", {"point_key": "c999", "point_code": "OTH"}, []]],
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match=r"Яндекс Путешествия: не нашёл id для XYZ"):
            await resolve("XYZ", client=client)


@pytest.mark.asyncio
async def test_file_cache_survives_memory_reset(tmp_path, monkeypatch):
    """Файловый кэш переживает сброс памяти процесса."""
    cache_file = tmp_path / "test_cache.json"
    monkeypatch.setenv("YANDEX_IDS_CACHE", str(cache_file))

    payload = [
        "uus",
        [[0, "Южно-Сахалинск", {"point_key": "c23242", "point_code": "UUS"}, []]],
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        res = await resolve("UUS", client=client)
        assert res == "c23242"

    assert cache_file.is_file()
    disk_data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert disk_data.get("UUS") == "c23242"

    # Сбрасываем память процесса
    clear_cache()
    assert "UUS" not in _MEM_CACHE

    # Повторный вызов без передачи client (без сети) читает из файла
    res2 = await resolve("UUS")
    assert res2 == "c23242"
    assert _MEM_CACHE.get("UUS") == "c23242"


@pytest.mark.asyncio
async def test_negative_cache_and_no_disk_caching_for_errors(tmp_path, monkeypatch):
    """Ошибки/не найденные id кэшируются в памяти (отрицательный кэш), но не пишутся на диск."""
    cache_file = tmp_path / "test_cache.json"
    monkeypatch.setenv("YANDEX_IDS_CACHE", str(cache_file))

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=["unknown", []])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="не нашёл id для UNK"):
            await resolve("UNK", client=client)
        assert call_count == 1

        # Повторный вызов не ходит в сеть (отрицательный кэш в памяти)
        with pytest.raises(RuntimeError, match="не нашёл id для UNK"):
            await resolve("UNK", client=client)
        assert call_count == 1

    # На диске файл не должен содержать UNK
    if cache_file.exists():
        disk_data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert "UNK" not in disk_data


@pytest.mark.asyncio
async def test_parallel_calls_single_network_request():
    """Параллельные вызовы для одного кода не шлют два запроса."""
    call_count = 0

    async def delayed_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json=["par", [[0, "Париж", {"point_key": "c10502", "point_code": "PAR"}, []]]],
        )

    class AsyncHandlerTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await delayed_handler(request)

    async with httpx.AsyncClient(transport=AsyncHandlerTransport()) as client:
        results = await asyncio.gather(
            resolve("PAR", client=client),
            resolve("PAR", client=client),
            resolve("PAR", client=client),
        )

    assert results == ["c10502", "c10502", "c10502"]
    assert call_count == 1


def test_city_id_synchronous_compatibility():
    """Синхронная city_id работает для статики и кэша в памяти, но бросает ошибку для остальных."""
    assert city_id("MOW") == "c213"
    assert city_id("led") == "c2"

    _MEM_CACHE["PAR"] = "c10502"
    assert city_id("PAR") == "c10502"

    with pytest.raises(RuntimeError, match="нет id в таблице yandex_ids.py"):
        city_id("NONEXISTENT")
