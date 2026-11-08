"""Ранняя карточка живого поиска (П31, флаг CARD_EARLY): карточка не ждёт самый медленный сайт.

Карточка уходит, как только сайтов с ценами не меньше CARD_EARLY_SITES, или в момент CARD_EARLY_SECONDS, если цены
к нему уже есть, — что раньше; поиск идёт дальше, в конце та же карточка правится на полную (bot.py).
Время проверяется один раз: к CARD_EARLY_SECONDS цен нет — по времени ранней карточки не будет, остаётся правило
сайтов, иначе одна карточка в конце, как без флага. Когда ждать уже некого (ответили все сайты живого поиска),
ранняя карточка не нужна: полная будет сразу.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
import logging
from typing import Any

from .criteria import SearchCriteria
from .filters import apply_criteria
from .models import Offer, SiteResult
from .offers import filter_by_baggage

log = logging.getLogger("avia.card_early")


def live_site_names(settings: Any) -> list[str]:
    """Сайты, которые живой поиск опрашивает на самом деле: включённые, без стадии trial (как pipeline.search)."""
    skip_trial = bool(getattr(settings, "trial_stage", True))
    return [
        s.name
        for s in settings.sites
        if s.enabled and not (skip_trial and getattr(s, "stage", "live") == "trial")
    ]


def sites_with_prices(results: Iterable[SiteResult]) -> int:
    """Сайтов с ценами — так же считает sites_ok в searches (metrics.record_search)."""
    return sum(1 for r in results if r.ok and r.offers)


def min_price(results: Iterable[SiteResult]) -> float | None:
    """Минимум цены по выдаче; None — цен нет."""
    prices = [o.price for r in results if r.ok for o in r.offers]
    return float(min(prices)) if prices else None


def shown_min_price(results: Sequence[SiteResult], criteria: SearchCriteria, baggage_mode: str) -> float | None:
    """Минимум цены среди того, что увидит человек: после его фильтров и багажа, как в карточке."""
    try:
        by_criteria, _ = apply_criteria(results, criteria)
        shown, _ = filter_by_baggage(by_criteria, baggage_mode)
    except Exception:
        log.exception("минимум цены для метрики карточки не посчитан")
        return None
    return min_price(shown)


@dataclass(frozen=True)
class CompactView:
    """Карточка живого поиска (compact-отчёт) и всё, что нужно снимку выдачи (store.put)."""

    chunks: list[str]
    flat: list[Offer]
    pool: list[Offer]
    filtered: list[SiteResult]
    dropped_criteria: int
    dropped_baggage: int

    @property
    def has_more(self) -> bool:
        """Есть билеты сверх показанных — кнопка «➕ Показать ещё»."""
        shown_ids = {o.offer_id for o in self.flat if o.offer_id}
        return any(o.offer_id not in shown_ids for o in self.pool)


@dataclass(frozen=True)
class EarlyCard:
    """Ранняя карточка, которая ушла человеку."""

    message_id: int | None  # None — Telegram не вернул id: править нечего, полная уйдёт новым сообщением
    dialog_id: str  # кнопки карточки; полная получит кнопки того же диалога
    seconds: float  # от старта поиска до карточки (searches.card_seconds)
    sites_ok: int  # сайтов с ценами в карточке (searches.card_sites_ok)
    min_price: float | None  # минимум цены в карточке (searches.card_min_price)


def card_metrics(
    early: EarlyCard | None,
    total_seconds: float,
    results: Sequence[SiteResult],
    criteria: SearchCriteria,
    baggage_mode: str,
) -> dict[str, Any]:
    """Колонки карточки для searches. Ранней карточки не было (флаг выключен или не пригодилась) — карточка
    и есть полный ответ: card_seconds = total_seconds, card_sites_ok = sites_ok, минимумы равны."""
    full_min = shown_min_price(results, criteria, baggage_mode)
    if early is None:
        return {
            "card_seconds": total_seconds,
            "card_sites_ok": sites_with_prices(results),
            "card_min_price": full_min,
            "full_min_price": full_min,
        }
    return {
        "card_seconds": early.seconds,
        "card_sites_ok": early.sites_ok,
        "card_min_price": early.min_price,
        "full_min_price": full_min,
    }


class SiteWatch:
    """Обёртка on_site_done живого поиска: кто ответил и когда пора слать раннюю карточку."""

    def __init__(
        self,
        on_site_done: Callable[[SiteResult], Awaitable[None]],
        live_sites: Sequence[str],
        *,
        early_seconds: float,
        early_sites: int,
    ) -> None:
        self._inner = on_site_done
        self.live_sites = tuple(live_sites)
        self.early_seconds = float(early_seconds)
        self.early_sites = int(early_sites)
        self.results: dict[str, SiteResult] = {}  # ответившие сайты; словарь заменяется целиком, не правится
        self.changed = asyncio.Event()
        self._time_checked = False
        self._time_armed = False  # к early_seconds цены были: карточка по времени

    async def on_site_done(self, res: SiteResult) -> None:
        self.results = {**self.results, res.site: res}
        self.changed.set()
        await self._inner(res)

    def pending(self) -> list[str]:
        """Сайты живого поиска, которые ещё не ответили, в порядке sites.yaml."""
        return [name for name in self.live_sites if name not in self.results]

    def due(self, elapsed: float) -> bool:
        """Пора ли слать раннюю карточку через elapsed секунд от старта поиска."""
        ok = sites_with_prices(self.results.values())
        if not self._time_checked and elapsed >= self.early_seconds:
            self._time_checked = True
            self._time_armed = ok >= 1
        if not self.pending():
            return False
        return ok >= self.early_sites or self._time_armed

    def next_timeout(self, elapsed: float, safety_left: float | None) -> float | None:
        """Сколько ждать следующего ответа сайта: до проверки времени и до предохранителя; None — без срока."""
        waits = [w for w in (safety_left, self.early_seconds - elapsed if not self._time_checked else None)
                 if w is not None]
        return max(0.0, min(waits)) if waits else None

    async def wait(self, search_task: asyncio.Task, timeout: float | None) -> None:
        """Ждёт ответа сайта, конца поиска или timeout — что раньше."""
        changed = asyncio.ensure_future(self.changed.wait())
        try:
            await asyncio.wait({search_task, changed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            changed.cancel()
