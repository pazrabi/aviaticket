"""Фоновый сбор цен по популярным маршрутам для накопления обучающей выборки."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import logging
import time
from typing import Any, Callable

from .config import Settings
from .history import PriceHistory, parse_seed_routes
from .models import Query
from .offers import assign_ids

log = logging.getLogger("avia.bg_collector")


class BackgroundCollector:
    """Фоновый сбор цен по популярным маршрутам в часы простоя."""

    def __init__(
        self,
        history: PriceHistory,
        search_fn: Callable[[Query], Any],
        is_idle: Callable[[], bool],
        settings: Settings,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.history = history
        self.search_fn = search_fn
        self.is_idle = is_idle
        self.s = settings
        self.settings = settings
        self._clock = clock
        self._today = today
        self._cur_date: date = self._today()
        self._done_today: int = 0
        self._slots: dict[tuple[str, str, int], date] = {}
        self._last_attempt_at: dict[tuple[str, str, date], float] = {}
        if isinstance(self.history, PriceHistory):
            try:
                self._sync_slots()
            except Exception:
                log.exception("ошибка первичной синхронизации слотов")

    def _check_date(self) -> None:
        cur = self._today()
        if cur != self._cur_date:
            self._cur_date = cur
            self._done_today = 0

    @property
    def done_today(self) -> int:
        self._check_date()
        return self._done_today

    def _get_routes(self) -> list[tuple[str, str]]:
        if hasattr(self.history, "get_bg_routes"):
            try:
                return self.history.get_bg_routes(
                    top_routes=self.s.bg_collect_top_routes,
                    seed_routes=self.s.bg_collect_seed_routes,
                    now=self._clock(),
                )
            except Exception:
                log.exception("ошибка получения популярных маршрутов")
        return parse_seed_routes(self.s.bg_collect_seed_routes)

    def _sync_slots(self) -> list[tuple[str, str, date]]:
        """Обновляет и возвращает активные пары (route, depart_date)."""
        if hasattr(self.history, "sync_bg_slots"):
            try:
                return self.history.sync_bg_slots(
                    today=self._today(),
                    top_routes=self.s.bg_collect_top_routes,
                    seed_routes=self.s.bg_collect_seed_routes,
                    horizons_days=self.s.bg_collect_horizons_days,
                    slots=self._slots,
                    now=self._clock(),
                )
            except Exception:
                log.exception("ошибка синхронизации слотов")
        return []

    @property
    def distinct_routes_count(self) -> int:
        return len(self._get_routes())

    @property
    def active_pairs_count(self) -> int:
        return len(self._sync_slots())

    @property
    def active_pairs(self) -> list[tuple[str, str, date]]:
        return self._sync_slots()

    @property
    def active_routes(self) -> list[tuple[str, str]]:
        return self._get_routes()

    def get_candidates(self, limit: int = 1) -> list[Query]:
        now = self._clock()
        recheck_s = float(self.s.bg_collect_recheck_hours * 3600.0)
        today = self._today()

        if isinstance(self.history, PriceHistory):
            return self.history.collect_slot_candidates(
                limit=limit,
                today=today,
                now=now,
                recheck_seconds=recheck_s,
                top_routes=self.s.bg_collect_top_routes,
                seed_routes=self.s.bg_collect_seed_routes,
                horizons_days=self.s.bg_collect_horizons_days,
                slots=self._slots,
                last_checked=self._last_attempt_at,
            )

        return self.history.collect_candidates(
            limit=limit,
            today=today,
            now=now,
            recheck_seconds=recheck_s,
        )

    def collect_candidates(self, limit: int = 1) -> list[Query]:
        """Алиас для get_candidates."""
        return self.get_candidates(limit=limit)

    async def run_once(self) -> bool:
        """Один шаг фонового сбора: ищет билет по популярному маршруту при условии простоя."""
        if self.done_today >= self.s.bg_collect_max_per_day:
            return False
        if not self.is_idle():
            return False

        now = self._clock()
        try:
            candidates = self.get_candidates(limit=1)
        except Exception:
            log.exception("ошибка получения кандидатов фонового сбора")
            return False

        if not candidates:
            return False

        q = candidates[0]
        # Фиксируем факт проверки слота независимо от наличия билетов в выдаче
        self._last_attempt_at[(q.origin.upper(), q.destination.upper(), q.depart)] = now
        if hasattr(self.history, "mark_bg_slot_checked"):
            try:
                self.history.mark_bg_slot_checked(q.origin, q.destination, q.depart, now)
            except Exception:
                pass

        try:
            results = await self.search_fn(q)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка фонового поиска")
            return False

        try:
            if results:
                assign_ids(results, now=now)
            await asyncio.to_thread(self.history.annotate, q, results, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка записи результатов фонового поиска")

        self._check_date()
        self._done_today += 1
        return True

    async def run_forever(self) -> None:
        """Бесконечный цикл: первый запуск через interval, затем каждые interval минут."""
        while True:
            try:
                interval = max(0.0, float(self.s.bg_collect_interval_minutes * 60))
                await asyncio.sleep(interval)
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка в цикле фонового сбора")
