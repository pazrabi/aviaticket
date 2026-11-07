"""Защита и надёжность поисковых запросов: ограничение частоты, кэш, повторы и friendly_error."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
import inspect
import time
from typing import Any, TypeVar

import httpx

T = TypeVar("T")

# Сетевые ошибки, при которых запрос повторяется один раз
RETRYABLE_EXCEPTIONS = (
    httpx.TransportError,
    httpx.TimeoutException,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


class _InFlight:
    """Состояние запроса в полёте для схлопывания одновременных одинаковых вызовов."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.result: Any = None
        self.exception: BaseException | None = None


class SearchGuard:
    """Управление надёжностью и частотой запросов к сайтам.

    - Кэширование успешных ответов на время cache_ttl (ошибки не кэшируются).
    - Очередь запросов на сайт (asyncio.Lock) с паузой не менее min_interval между началами поисков.
    - Разные сайты не блокируют друг друга.
    - Схлопывание одновременных одинаковых запросов (второй ждёт первый и берёт результат из кэша).
    - Повтор при сетевой ошибке один раз с паузой retry_delay.
    """

    def __init__(
        self,
        min_interval: float = 30.0,
        cache_ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
        retry_delay: float = 2.0,
    ) -> None:
        self.min_interval = float(min_interval)
        self.cache_ttl = float(cache_ttl)
        self.clock = clock
        self.sleep = sleep
        self.retry_delay = float(retry_delay)

        self._cache: dict[tuple[str, Hashable], tuple[float, Any]] = {}
        self._inflight: dict[tuple[str, Hashable], _InFlight] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_starts: dict[str, float] = {}

    def _get_lock(self, site: str) -> asyncio.Lock:
        lock = self._locks.get(site)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[site] = lock
        return lock

    async def _do_sleep(self, seconds: float) -> None:
        res = self.sleep(seconds)
        if inspect.isawaitable(res):
            await res

    def _get_cached(self, cache_key: tuple[str, Hashable]) -> tuple[bool, Any]:
        """Возвращает (найдено, значение) из кэша с проверкой TTL."""
        if cache_key in self._cache:
            ts, val = self._cache[cache_key]
            if self.clock() - ts < self.cache_ttl:
                return True, val
            del self._cache[cache_key]
        return False, None

    async def run(
        self,
        key_site: str,
        key_query: Hashable,
        factory: Callable[[], Awaitable[T]],
        retry_delay: float | None = None,
        live: bool = False,
        cache_if: Callable[[Any], bool] | None = None,
    ) -> tuple[T, bool]:
        """Выполняет поиск через factory с кэшированием, защитой сайта и повтором при сбое."""
        cache_key = (key_site, key_query)

        while True:
            # 1. Быстрая проверка кэша без ожидания блокировок
            found, cached_val = self._get_cached(cache_key)
            if found:
                return cached_val, True

            # 2. Если одинаковый запрос уже в полёте — ждём его и берём результат
            flight = self._inflight.get(cache_key)
            if flight is not None:
                await flight.event.wait()
                if flight.exception is not None:
                    # Если первичный запрос был отменён извне, ожидающий запрос повторяет поиск сам
                    if isinstance(flight.exception, asyncio.CancelledError):
                        if self._inflight.get(cache_key) is flight:
                            self._inflight.pop(cache_key, None)
                        continue
                    raise flight.exception
                return flight.result, True

            # Регистрируем новый полёт для одинаковых одновременных запросов
            flight = _InFlight()
            self._inflight[cache_key] = flight
            break

        delay = self.retry_delay if retry_delay is None else float(retry_delay)

        try:
            if live:
                # Живой поиск человека не ждёт ни замок сайта, ни паузу: телефоны делят работу через очередь хаба
                result = await self._run_live(factory, delay)
                if cache_if is None or cache_if(result):
                    self._cache[cache_key] = (self.clock(), result)
                flight.result = result
                flight.event.set()
                return result, False

            # Запросы одного сайта выполняются строго по очереди
            async with self._get_lock(key_site):
                # Перепроверяем кэш внутри очереди сайта
                found, cached_val = self._get_cached(cache_key)
                if found:
                    flight.result = cached_val
                    flight.event.set()
                    return cached_val, True

                # Выдерживаем min_interval между началами двух реальных поисков одного сайта
                last_start = self._last_starts.get(key_site)
                if last_start is not None:
                    elapsed = self.clock() - last_start
                    if elapsed < self.min_interval:
                        await self._do_sleep(self.min_interval - elapsed)

                self._last_starts[key_site] = self.clock()

                # Реальный поиск с одним повтором при сетевой ошибке
                try:
                    result = await factory()
                except RETRYABLE_EXCEPTIONS:
                    await self._do_sleep(delay)
                    result = await factory()

                # Кэшируем только успешный результат (ошибки не кэшируются)
                if cache_if is None or cache_if(result):
                    self._cache[cache_key] = (self.clock(), result)
                flight.result = result
                flight.event.set()
                return result, False

        except BaseException as exc:
            flight.exception = exc
            flight.event.set()
            raise
        finally:
            self._inflight.pop(cache_key, None)

    async def _run_live(self, factory: Callable[[], Awaitable[T]], delay: float) -> T:
        try:
            return await factory()
        except RETRYABLE_EXCEPTIONS:
            await self._do_sleep(delay)
            return await factory()

    def clear(self) -> None:
        """Очистить кэш, состояние ожиданий и блокировки."""
        self._cache.clear()
        self._inflight.clear()
        self._last_starts.clear()
        self._locks.clear()


def friendly_error(exc: BaseException) -> str:
    """Понятное русское сообщение для Telegram без технических деталей и секретов."""
    text = str(exc).lower()

    # Сайт показал капчу или блокировку
    if "капч" in text or "блокировк" in text:
        return "сайт показал капчу, владельцу нужно пройти её руками: python -m avia.recon <сайт> ... --show"

    # Таймаут ожидания ответа
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)) or "таймаут" in text:
        return "таймаут"

    # Сетевые ошибки (разрыв соединения, недоступность сети, DNS и т.д.)
    if isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
        return "нет сети"

    # Неизвестная ошибка: только тип исключения, без технического текста и секретов
    return f"неизвестная ошибка ({type(exc).__name__})"
