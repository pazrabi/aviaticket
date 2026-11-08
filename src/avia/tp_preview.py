"""Предварительные цены из кэша Aviasales в сообщении ожидания живого поиска (П32, флаг TP_PREVIEW).

Вместе со стартом живого поиска — отдельная задача: запрос prices_for_dates (Travelpayouts Data API,
fetch_travelpayouts владельца) по тому же маршруту, дате, взрослым и валюте. Цены пришли не позже
TP_PREVIEW_TIMEOUT — под шапкой сообщения ожидания одна строка: минимум кэша после фильтров человека, авиакомпания,
время вылета и пометка, что это кэш. Позже, пусто, ошибка (401, нет сети и т. п.) — строки нет, причина в лог;
поиск кэш не ждёт. В конце поиска задача снимается (stop_preview).

Цены кэша — только в этой строке: в карточку, снимок выдачи, историю цен и метрики сайтов они не идут.
О багаже кэш не знает (baggage у билетов пуст), поэтому по багажу строку не фильтруем.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from html import escape
import logging
import time
from typing import Any, Protocol

from .criteria import SearchCriteria
from .filters import apply_criteria
from .formatting import money
from .models import Offer, Query, SiteResult
from .parsers import travelpayouts

log = logging.getLogger("avia.tp_preview")

PREVIEW_NOTE = "по кэшу Aviasales, цена может отличаться"
# сколько самых дешёвых билетов просить у кэша: фильтры человека могут отбросить первые
PREVIEW_LIMIT = 30
# сколько ждать снятую задачу кэша в конце поиска: дольше — только лог, конец поиска не держим
STOP_WAIT_SECONDS = 2.0


class PreviewTarget(Protocol):
    """Сообщение ожидания (bot.WaitMessageUpdater): показывает строку под шапкой."""

    async def show_preview(self, line: str) -> bool: ...


@dataclass(frozen=True)
class Preview:
    """Строка кэша, которую увидел человек."""

    seconds: float  # от старта поиска до показа (searches.preview_seconds)
    min_price: float  # минимум в строке (searches.preview_min_price)


def preview_enabled(settings: Any) -> bool:
    """Предпросмотр включён: TP_PREVIEW=1 и есть TP_TOKEN. Без токена запросов нет."""
    return bool(getattr(settings, "tp_preview", False)) and bool(getattr(settings, "tp_token", ""))


def missing_token(settings: Any) -> bool:
    """TP_PREVIEW=1, а TP_TOKEN пуст — одна строка в лог при старте бота."""
    return bool(getattr(settings, "tp_preview", False)) and not getattr(settings, "tp_token", "")


def _one_leg_view(offers: Sequence[Offer], q: Query) -> list[Offer]:
    """Туда-обратно: duration в ответе — сумма обоих плеч, а фильтр «в пути до N ч» меряет каждое плечо.

    Сумму за длительность «туда» не выдаём: длительность неизвестна, и фильтр её пропускает, как в карточке.
    """
    if q.return_date is None:
        return list(offers)
    return [replace(o, duration=None) for o in offers]


def cheapest(offers: Sequence[Offer], q: Query, criteria: SearchCriteria) -> Offer | None:
    """Самый дешёвый билет кэша после фильтров человека (apply_criteria, как в карточке); None — не осталось."""
    if not offers:
        return None
    kept, _ = apply_criteria([SiteResult(offers[0].site, True, _one_leg_view(offers, q))], criteria)
    priced = [o for r in kept for o in r.offers if o.price > 0]
    return min(priced, key=lambda o: o.price, default=None)


def preview_line(offer: Offer) -> str:
    """Строка под шапкой: «⚡ Предварительно: от 6 900 ₽ · DP · вылет 23:40 (по кэшу Aviasales, …)»."""
    parts = [f"от {money(offer.price, offer.currency)}"]
    if offer.airline:
        parts.append(escape(offer.airline))
    if offer.depart_time:
        parts.append(f"вылет {escape(offer.depart_time)}")
    return f"⚡ Предварительно: {' · '.join(parts)} <i>({PREVIEW_NOTE})</i>"


async def run_preview(
    target: PreviewTarget,
    q: Query,
    criteria: SearchCriteria,
    settings: Any,
    t_search0: float,
    chat_id: int,
) -> Preview | None:
    """Задача предпросмотра: кэш → строка в сообщении ожидания. Ошибки — в лог, наружу не идут."""
    timeout = float(getattr(settings, "tp_preview_timeout", 3.0))
    try:
        offers = await asyncio.wait_for(
            travelpayouts.fetch_travelpayouts(q, settings.tp_token, settings.tp_marker, limit=PREVIEW_LIMIT),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.info("кэш Aviasales не ответил за %.1f с, chat_id=%s: строки предпросмотра нет", timeout, chat_id)
        return None
    except Exception as e:
        log.warning("кэш Aviasales: %s, chat_id=%s: строки предпросмотра нет", e, chat_id)
        return None
    best = cheapest(offers, q, criteria)
    if best is None:
        where = "после фильтров " if offers else ""
        log.info("в кэше Aviasales %sбилетов нет, chat_id=%s: строки предпросмотра нет", where, chat_id)
        return None
    if not await target.show_preview(preview_line(best)):
        log.info("строка кэша Aviasales не показана, chat_id=%s", chat_id)
        return None
    preview = Preview(seconds=time.monotonic() - t_search0, min_price=float(best.price))
    log.info("строка кэша Aviasales, chat_id=%s: %.1f с, от %s", chat_id, preview.seconds, best.price)
    return preview


async def stop_preview(task: asyncio.Task | None) -> Preview | None:
    """Конец поиска: снимает задачу кэша, если она ещё идёт, и отдаёт показанную строку (None — не было)."""
    if task is None:
        return None
    if not task.done():
        task.cancel()
        await asyncio.wait([task], timeout=STOP_WAIT_SECONDS)
    if not task.done():
        log.warning("задача кэша Aviasales не снялась за %.0f с", STOP_WAIT_SECONDS)
        return None
    if task.cancelled():
        return None
    exc = task.exception()
    if exc is not None:  # run_preview ловит всё сама; сюда — только ошибка в ней самой
        log.error("задача кэша Aviasales упала: %r", exc)
        return None
    return task.result()


def preview_metrics(preview: Preview | None) -> dict[str, float | None]:
    """Колонки предпросмотра для searches: не было строки — NULL."""
    if preview is None:
        return {"preview_seconds": None, "preview_min_price": None}
    return {"preview_seconds": preview.seconds, "preview_min_price": preview.min_price}
