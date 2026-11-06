"""Модуль хеджей для живого поиска (шаг 9 PLAN_SPEED.md).

Хеджирование: для живого поиска (priority 0) при рискованной паре (сайт + телефон)
запускается вторая копия запроса на свободном телефоне. Первый успешный результат побеждает,
проигравшая копия отменяется, а её телефон освобождается.
"""
from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
import time
from typing import Any, Iterable

from .config import Settings

logger = logging.getLogger("avia.hedge")


def _calc_percentile(data: list[float], p: float) -> float:
    """Вычисляет p-й процентиль (0..100) с линейной интерполяцией."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    k = (n - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return d0 + d1


def get_hedge_stats(
    site: str | Any,
    device: str,
    settings: Settings,
    now: float | None = None,
) -> dict[str, Any]:
    """Возвращает статистику попыток за 7 дней для пары (сайт, телефон).

    Статистика включает:
    - total attempts
    - fail_share
    - p90 fetch_seconds
    - p50 fetch_seconds (медиана)
    - флаги is_fail_risky, is_slow_risky, is_risky
    """
    default_res: dict[str, Any] = {
        "is_risky": False,
        "is_fail_risky": False,
        "is_slow_risky": False,
        "fail_share": 0.0,
        "attempts": 0,
        "p90": 0.0,
        "p50": 0.0,
    }
    try:
        if not getattr(settings, "hedge", True):
            return default_res

        site_name = getattr(site, "name", site)
        if not site_name or not device:
            return default_res

        metrics = getattr(settings, "metrics", None)
        if metrics is None:
            storage_dir = getattr(settings, "storage_dir", None)
            if storage_dir is not None:
                metrics_path = Path(storage_dir) / "metrics.sqlite"
                if metrics_path.exists():
                    from .metrics import MetricsStore

                    metrics = MetricsStore(metrics_path)

        if metrics is None:
            return default_res

        cur_now = time.time() if now is None else float(now)
        since_ts = cur_now - 7 * 86400.0

        fail_share_threshold = float(getattr(settings, "hedge_fail_share", 0.15))
        min_attempts = int(getattr(settings, "hedge_min_attempts", 5))
        live_safety = float(getattr(settings, "live_safety_seconds", 90.0))
        safety_half = live_safety / 2.0

        with metrics._connect() as conn:
            rows = conn.execute(
                """
                SELECT outcome, offers, fetch_seconds
                FROM attempts
                WHERE site = ? AND device = ? AND ts >= ?
                ORDER BY ts ASC
                """,
                (str(site_name), str(device), since_ts),
            ).fetchall()

        total_cnt = len(rows)
        fail_cnt = 0
        durations: list[float] = []

        for r in rows:
            outc = (r["outcome"] or "").lower()
            off = int(r["offers"] or 0)
            if outc != "ok" and not (outc == "partial" and off > 0):
                fail_cnt += 1

            fs = r["fetch_seconds"]
            if fs is not None and float(fs) > 0:
                durations.append(float(fs))

        fail_share = (fail_cnt / total_cnt) if total_cnt > 0 else 0.0
        p90 = _calc_percentile(durations, 90.0) if durations else 0.0
        p50 = _calc_percentile(durations, 50.0) if durations else 0.0

        is_fail_risky = bool(total_cnt >= min_attempts and fail_share >= fail_share_threshold)
        is_slow_risky = bool(p90 > safety_half)
        is_risky = is_fail_risky or is_slow_risky

        return {
            "is_risky": is_risky,
            "is_fail_risky": is_fail_risky,
            "is_slow_risky": is_slow_risky,
            "fail_share": fail_share,
            "attempts": total_cnt,
            "p90": p90,
            "p50": p50,
        }
    except Exception as e:
        logger.warning("Ошибка чтения метрик для get_hedge_stats (%s, %s): %s", site, device, e)
        return default_res


def hedge_risky(
    site: str | Any,
    device: str,
    settings: Settings,
    now: float | None = None,
) -> bool:
    """Решение о хедже: пара сайт+телефон «рискованная», если по attempts за 7 дней

    доля неудач >= HEDGE_FAIL_SHARE (0.15) при >= 5 попыток ИЛИ p90 времени > LIVE_SAFETY_SECONDS/2.
    При любой ошибке возвращает False.
    """
    try:
        if not getattr(settings, "hedge", True):
            return False
        st = get_hedge_stats(site, device, settings, now=now)
        return bool(st.get("is_risky", False))
    except Exception as e:
        logger.warning("Ошибка проверки hedge_risky для (%s, %s): %s", site, device, e)
        return False


class HedgeTracker:
    """Отслеживает лимиты хеджей на поиск: не более 1 на сайт и не более max_hedges на поиск."""

    def __init__(self, max_hedges: int = 2) -> None:
        self.max_hedges = max_hedges
        self.count = 0
        self._sites_hedged: set[str] = set()
        self._lock = asyncio.Lock()

    async def try_acquire(self, site_name: str) -> bool:
        async with self._lock:
            clean = site_name.strip()
            if clean in self._sites_hedged:
                return False
            if self.count >= self.max_hedges:
                return False
            self.count += 1
            self._sites_hedged.add(clean)
            return True

    async def release(self, site_name: str) -> None:
        async with self._lock:
            clean = site_name.strip()
            if clean in self._sites_hedged:
                self._sites_hedged.remove(clean)
                self.count = max(0, self.count - 1)
