"""Фоновый сбор цен по популярным маршрутам для накопления обучающей выборки."""
from __future__ import annotations

import asyncio
from datetime import date
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

    def _check_date(self) -> None:
        cur = self._today()
        if cur != self._cur_date:
            self._cur_date = cur
            self._done_today = 0

    @property
    def done_today(self) -> int:
        self._check_date()
        return self._done_today

    async def run_once(self) -> bool:
        """Один шаг фонового сбора: ищет билет по популярному маршруту при условии простоя."""
        if self.done_today >= self.s.bg_collect_max_per_day:
            return False
        if not self.is_idle():
            return False

        now = self._clock()
        recheck_s = self.s.bg_collect_recheck_hours * 3600.0
        candidates = self.history.collect_candidates(
            limit=1,
            today=self._today(),
            now=now,
            recheck_seconds=recheck_s,
        )
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
