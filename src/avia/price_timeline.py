"""Хронология цен по ходу загрузки страницы."""
from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import time
from typing import Any

from .collector import Snapshot
from .config import Settings, SiteConfig
from .models import Query
from .parsers import PARSERS

logger = logging.getLogger(__name__)


def _compute_timeline(
    site: SiteConfig,
    snap: Snapshot,
    q: Query,
    settings: Settings,
) -> dict[str, Any] | None:
    """Синхронный разбор префиксов ответов для построения хронологии цен."""
    if not snap or not snap.json_payloads:
        return None

    times = getattr(snap, "payload_times", None) or []
    if not any(t is not None for t in times):
        return None

    # Собираем элементы (t, url, body) только для тех ответов, где t известен
    valid_items: list[tuple[float | int, str, str]] = []
    for idx, (url, body) in enumerate(snap.json_payloads):
        t = times[idx] if idx < len(times) else None
        if t is not None:
            valid_items.append((t, url, body))

    if not valid_items:
        return None

    # Сортируем в порядке роста t
    valid_items.sort(key=lambda x: x[0])

    # Группируем по моментам прихода
    grouped: list[tuple[float | int, list[tuple[str, str]]]] = []
    for t_val, grp in itertools.groupby(valid_items, key=lambda x: x[0]):
        grouped.append((t_val, [(url, body) for _, url, body in grp]))

    # Формируем накопленные префиксы payloads
    prefixes: list[tuple[float | int, list[tuple[str, str]]]] = []
    accumulated: list[tuple[str, str]] = []
    for t_val, grp_payloads in grouped:
        accumulated = accumulated + grp_payloads
        prefixes.append((t_val, accumulated))

    # Не больше 40 точек (если ответов больше, брать последние 40 префиксов, но всегда включать последнюю)
    if len(prefixes) > 40:
        prefixes = prefixes[-40:]

    parser = PARSERS.get(site.parser) if site and site.parser else None
    if parser is None:
        return None

    points: list[dict[str, Any]] = []
    t_start = time.monotonic()

    for t_last_prefix, prefix_payloads in prefixes:
        # Контроль общего времени разбора внутри потока
        if time.monotonic() - t_start > 5.0:
            logger.warning("Построение хронологии цен прервано по внутреннему таймауту 5 с")
            return None

        prefix_snap = Snapshot(
            url=snap.url,
            text=snap.text,
            json_payloads=prefix_payloads,
            blocked=snap.blocked,
        )

        try:
            if inspect.iscoroutinefunction(parser):
                offers = asyncio.run(parser(prefix_snap, q))
            else:
                offers = parser(prefix_snap, q)
        except Exception:
            offers = []

        if not offers or not isinstance(offers, list):
            offers = []

        prices: list[float | int] = []
        for o in offers:
            p = getattr(o, "price", None)
            if p is None and isinstance(o, dict):
                p = o.get("price")
            if p is None and isinstance(o, (int, float)):
                p = o
            if isinstance(p, (int, float)) and not isinstance(p, bool):
                prices.append(p)

        min_price = min(prices) if prices else None
        points.append({
            "t": t_last_prefix,
            "min": min_price,
            "n": len(offers),
        })

    if not points:
        return None

    return {
        "points": points,
        "t_first_match": getattr(snap, "t_first_match", None),
        "woke": getattr(snap, "woke", None),
        "final_min": points[-1]["min"],
        "t_last": points[-1]["t"],
    }


async def build_timeline(
    site: SiteConfig,
    snap: Snapshot,
    q: Query,
    settings: Settings,
) -> dict[str, Any] | None:
    """Строит хронологию цен по ходу загрузки страницы в отдельном потоке (до 5 секунд)."""
    if not snap or not snap.json_payloads:
        return None
    times = getattr(snap, "payload_times", None) or []
    if not any(t is not None for t in times):
        return None

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_compute_timeline, site, snap, q, settings),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Построение price_timeline не уложилось в таймаут 5 с")
        return None
    except Exception as exc:
        logger.warning("Ошибка при построении price_timeline: %s", exc)
        return None
