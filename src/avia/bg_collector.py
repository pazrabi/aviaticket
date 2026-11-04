"""Фоновый сбор цен по популярным маршрутам для накопления обучающей выборки."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import logging
import time
from typing import Any, Callable

from .config import Settings
from .history import PriceHistory
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
        if hasattr(self.history, "get_bg_slots"):
            try:
                self._slots.update(self.history.get_bg_slots())
            except Exception:
                pass

    def _check_date(self) -> None:
        cur = self._today()
        if cur != self._cur_date:
            self._cur_date = cur
            self._done_today = 0

    @property
    def done_today(self) -> int:
        self._check_date()
        return self._done_today

    def _parse_seed_routes(self) -> list[tuple[str, str]]:
        routes: list[tuple[str, str]] = []
        seed_list = self.s.bg_collect_seed_routes
        if seed_list:
            for item in seed_list:
                if isinstance(item, str):
                    for part in item.split(","):
                        part = part.strip()
                        if "-" in part:
                            o, d = part.split("-", 1)
                            o, d = o.strip().upper(), d.strip().upper()
                            if o and d:
                                routes.append((o, d))
                                routes.append((d, o))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    o, d = str(item[0]).strip().upper(), str(item[1]).strip().upper()
                    if o and d:
                        routes.append((o, d))
                        routes.append((d, o))
        return routes

    def _get_routes(self) -> list[tuple[str, str]]:
        now = self._clock()
        pop_routes: list[tuple[str, str, int]] = []
        if hasattr(self.history, "popular_routes"):
            try:
                pop_routes = self.history.popular_routes(
                    limit=self.s.bg_collect_top_routes,
                    days=30,
                    now=now,
                )
            except Exception:
                log.exception("ошибка получения популярных маршрутов")

        routes: list[tuple[str, str]] = []
        seen = set()
        for orig, dest, _ in pop_routes:
            pair = (orig.upper(), dest.upper())
            if pair not in seen:
                seen.add(pair)
                routes.append(pair)

        for pair in self._parse_seed_routes():
            if pair not in seen:
                seen.add(pair)
                routes.append(pair)

        return routes

    def _sync_slots(self) -> list[tuple[str, str, date]]:
        """Обновляет и возвращает активные пары (route, depart_date)."""
        today = self._today()
        routes = self._get_routes()
        horizons = self.s.bg_collect_horizons_days or [7, 14, 28, 56, 90]

        if hasattr(self.history, "get_bg_slots"):
            try:
                db_slots = self.history.get_bg_slots()
                for k, v in db_slots.items():
                    if k not in self._slots:
                        self._slots[k] = v
            except Exception:
                pass

        active_pairs: list[tuple[str, str, date]] = []
        seen_pairs: set[tuple[str, str, date]] = set()

        for orig, dest in routes:
            for h in horizons:
                key = (orig, dest, h)
                cur_date = self._slots.get(key)
                if cur_date is None or cur_date < today:
                    cur_date = today + timedelta(days=h)
                    self._slots[key] = cur_date
                    if hasattr(self.history, "set_bg_slot"):
                        try:
                            self.history.set_bg_slot(orig, dest, h, cur_date)
                        except Exception:
                            pass
                pair = (orig, dest, cur_date)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    active_pairs.append(pair)
        return active_pairs

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
