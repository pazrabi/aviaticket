"""Кеш свежих данных поисков по сайтам для быстрого режима."""
from __future__ import annotations

from collections import OrderedDict
import copy
from datetime import date
import logging
import threading
import time
from typing import Callable

from .models import Query, SiteResult

log = logging.getLogger("avia.fresh_cache")

DEFAULT_FRESH_TTL_SECONDS: int = 900  # 15 минут
DEFAULT_FRESH_MAX_ENTRIES: int = 300


class FreshCache:
    """В памяти, потокобезопасный кеш свежих результатов отдельных сайтов с ограничением по размеру и TTL."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_FRESH_TTL_SECONDS,
        max_entries: int = DEFAULT_FRESH_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, tuple[SiteResult, float]] = OrderedDict()

    @staticmethod
    def _make_key(query: Query | tuple, site_name: str) -> tuple:
        site = str(site_name).strip().lower()
        if isinstance(query, tuple):
            return (site, *query)
        orig = getattr(query, "origin", "")
        dest = getattr(query, "destination", "")
        dep = getattr(query, "depart", None)
        ret = getattr(query, "return_date", None)
        adults = getattr(query, "adults", 1)
        curr = getattr(query, "currency", "RUB")
        return (
            site,
            str(orig).strip().upper(),
            str(dest).strip().upper(),
            dep,
            ret,
            int(adults),
            str(curr or "RUB").strip().upper(),
        )

    def get(
        self,
        query: Query | tuple,
        site_name: str,
        max_age: float,
        now: float | None = None,
    ) -> tuple[SiteResult, float] | None:
        """Возвращает (SiteResult_копия, age_seconds) или None, если запись отсутствует или старше max_age/ttl."""
        if max_age <= 0:
            return None
        current_time = self._clock() if now is None else float(now)
        key = self._make_key(query, site_name)
        with self._lock:
            if key not in self._cache:
                return None
            cached_res, saved_at = self._cache[key]
            age = max(0.0, current_time - saved_at)
            if age > self.ttl_seconds:
                del self._cache[key]
                return None
            if age > max_age:
                return None
            self._cache.move_to_end(key)
            try:
                res = copy.deepcopy(cached_res)
            except Exception as e:
                log.warning("Ошибка копирования SiteResult из fresh_cache: %s", e)
                return None
            res.cached_age = age
            for o in res.offers:
                setattr(o, "cached_age", age)
            return res, age

    def put(
        self,
        query: Query | tuple,
        site_name: str,
        result: SiteResult,
        ts: float | None = None,
    ) -> bool:
        """Сохраняет SiteResult в кеш, если результат успешен и без ошибок/пустых предложений."""
        if not getattr(result, "ok", False):
            return False
        offers = getattr(result, "offers", None)
        if not offers:
            return False
        if getattr(result, "error", None):
            return False

        current_time = self._clock() if ts is None else float(ts)
        try:
            cached_res = copy.deepcopy(result)
        except Exception as e:
            log.warning("Ошибка копирования SiteResult для fresh_cache: %s", e)
            return False

        key = self._make_key(query, site_name)
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = (cached_res, current_time)
        return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __contains__(self, item: tuple[Query | tuple, str]) -> bool:
        query, site = item
        key = self._make_key(query, site)
        with self._lock:
            return key in self._cache

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
