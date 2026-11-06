"""Кеш полной выдачи глубокого поиска авиабилетов."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date
import threading
import time
from typing import Callable, Sequence

from .models import Query, SiteResult


class DeepCache:
    """Потокобезопасный кеш полной выдачи с ограничением по размеру (LRU/FIFO) и TTL."""

    def __init__(
        self,
        ttl_seconds: int = 900,
        max_entries: int = 100,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, tuple[list[SiteResult], float]] = OrderedDict()

    @staticmethod
    def _make_key(query: Query | tuple) -> tuple:
        if isinstance(query, tuple):
            return query
        return (
            query.origin,
            query.destination,
            query.depart,
            query.return_date,
            int(query.adults),
        )

    def get(
        self,
        query: Query | tuple,
        now: float | None = None,
    ) -> tuple[list[SiteResult], float] | None:
        """Возвращает (results, age_seconds) или None, если запись отсутствует или старше ttl."""
        key = self._make_key(query)
        current_time = self._clock() if now is None else now
        with self._lock:
            if key not in self._cache:
                return None
            results, saved_at = self._cache[key]
            age = max(0.0, current_time - saved_at)
            if age > self.ttl_seconds:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return list(results), age

    def put(
        self,
        query: Query | tuple,
        results: Sequence[SiteResult],
        now: float | None = None,
    ) -> None:
        """Сохраняет результаты глубокого поиска в кеш, вытесняя старейшие при превышении лимита."""
        key = self._make_key(query)
        current_time = self._clock() if now is None else now
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = (list(results), current_time)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __contains__(self, query: Query | tuple) -> bool:
        return self.get(query) is not None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
