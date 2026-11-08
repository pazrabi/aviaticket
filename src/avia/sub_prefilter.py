"""Предфильтр подписок через кэш Aviasales (КТ4, работа 2; флаг SUB_PREFILTER и TP_TOKEN).

Решает только одно: собирать ли подписку на телефонах в этот раз. Перед сбором группы подписок одного маршрута
планировщик спрашивает check(); как идёт сбор, предфильтр не меняет.

- Страховочный полный сбор: срок — SUB_PREFILTER_FULL_HOURS, по умолчанию 2 интервала опроса подписки
  (poll_interval_seconds владельца). Если до следующей проверки срок истечёт (прошло с последней собранной цены
  + ожидаемый промежуток до следующей проверки > срока), собираем сейчас и кэш не спрашиваем. Ожидаемый промежуток —
  прошлый, но не меньше интервала: если планировщик опоздает сильнее (очередь групп, простой бота), срок превысится.
- Иначе — один запрос prices_for_dates (fetch_travelpayouts владельца) на маршрут, не дольше TP_PREVIEW_TIMEOUT.
  Цена кэша — минимум в валюте подписки после её фильтров (tp_preview.cheapest, как строка П32).
  - отличается от последней собранной цены подписки на ≥ SUB_PREFILTER_PCT % или кэш маршрута не знает — собираем;
  - у подписки есть порог человека, и цена кэша или последняя собранная не выше его — собираем: «ниже вашего порога»
    не ждёт страховочного сбора;
  - у всех подписок группы изменения нет — сбор пропускаем (планировщик переносит срок, как после проверки).
- Ошибка, таймаут, лимит API — собираем, как без предфильтра; WARNING не чаще раза в 10 минут.
  После ответа 429 (лимит) кэш 10 минут не спрашиваем.

Счётчик решений — в памяти (после перезапуска с нуля), строка в /admin → «Парсинг» под «Источниками».
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import logging
import time
from typing import Any

from . import tp_preview
from .models import Offer
from .parsers import travelpayouts
from .subscriptions import Subscription, SubscriptionStore, poll_interval_seconds

log = logging.getLogger("avia.sub_prefilter")

SKIP = "skip"  # сбор пропущен: кэш изменения не показывает
CHANGED = "changed"  # кэш отличается от последней собранной цены
THRESHOLD = "threshold"  # цена кэша или последняя собранная не выше порога человека
UNKNOWN = "unknown"  # кэш маршрута не знает (пусто, всё отсеяно фильтрами, другая валюта)
FULL = "full"  # страховочный сбор: срок истекает или собранной цены нет
ERROR = "error"  # ошибка, таймаут или лимит API
REASONS = (SKIP, CHANGED, THRESHOLD, UNKNOWN, FULL, ERROR)

WARN_EVERY_SECONDS = 600.0  # ошибки кэша — не больше одной строки WARNING за столько секунд
LIMIT_PAUSE_SECONDS = 600.0  # после ответа 429 кэш не спрашиваем столько секунд
FULL_INTERVALS = 2  # срок страховки по умолчанию, в интервалах опроса подписки
KEEP_SECONDS = 7 * 86400.0 + 3600.0  # решения в памяти — на окно «7 дн» в /admin


@dataclass(frozen=True)
class Decision:
    collect: bool
    reason: str


def prefilter_enabled(settings: Any) -> bool:
    """Предфильтр включён: SUB_PREFILTER=1 и есть TP_TOKEN. Без токена — сбор как сейчас."""
    return bool(getattr(settings, "sub_prefilter", False)) and bool(getattr(settings, "tp_token", ""))


def missing_token(settings: Any) -> bool:
    """SUB_PREFILTER=1, а TP_TOKEN пуст — одна строка WARNING при старте бота."""
    return bool(getattr(settings, "sub_prefilter", False)) and not getattr(settings, "tp_token", "")


def full_seconds(settings: Any, interval: float) -> float:
    """Срок страховочного сбора: SUB_PREFILTER_FULL_HOURS или, если 0, 2 интервала опроса подписки."""
    hours = float(getattr(settings, "sub_prefilter_full_hours", 0.0) or 0.0)
    return hours * 3600.0 if hours > 0 else FULL_INTERVALS * float(interval)


def expected_gap(interval: float, now: float, last_checked_at: float | None) -> float:
    """Через сколько ждать следующую проверку: как прошлый промежуток, но не меньше обещанного интервала."""
    if last_checked_at is None:
        return float(interval)
    return max(float(interval), now - last_checked_at)


def safety_window(full: float, gap: float) -> float:
    """Пропуск можно, только если последняя собранная цена не старше этого окна: следующая проверка успеет до срока."""
    return full - gap


def price_changed(cache_price: float, last_price: float, pct: float) -> bool:
    """Кэш отличается от последней собранной цены на ≥ pct % (вверх или вниз)."""
    if last_price <= 0:
        return True
    return abs(cache_price - last_price) * 100.0 >= pct * last_price


def decide(last_price: int | None, cache_price: float | None, pct: float, threshold: int | None = None) -> Decision:
    """Решение по одной подписке.

    last_price — последняя собранная цена за окно страховки (None — окна нет: страховочный сбор);
    cache_price — минимум кэша после фильтров подписки (None — кэш маршрута не знает);
    threshold — порог человека («ниже вашего порога»), None — порога нет.
    """
    if last_price is None:
        return Decision(True, FULL)
    if cache_price is None:
        return Decision(True, UNKNOWN)
    if price_changed(cache_price, last_price, pct):
        return Decision(True, CHANGED)
    if threshold is not None and min(cache_price, last_price) <= threshold:
        return Decision(True, THRESHOLD)
    return Decision(False, SKIP)


def _error_text(exc: BaseException) -> str:
    """Причина для лога без адреса запроса: у HTTP-ошибки — только код."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}" + (" (лимит запросов, кэш не спрашиваем 10 мин)" if status == 429 else "")
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class SubPrefilter:
    """Предфильтр планировщика подписок: check() перед сбором группы, счётчик решений для /admin."""

    def __init__(
        self,
        store: SubscriptionStore | None,
        settings: Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        started_at: float | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.pct = float(getattr(settings, "sub_prefilter_pct", 5.0))
        self.timeout = float(getattr(settings, "tp_preview_timeout", 3.0))
        self.monotonic = monotonic
        self.started_at = time.time() if started_at is None else float(started_at)
        self._events: deque[tuple[float, str]] = deque()
        self._warned_at: float | None = None
        self._quiet_errors = 0
        self._limited_until: float | None = None  # после 429 — до этого момента (monotonic) кэш не спрашиваем

    async def check(self, subs: Sequence[Subscription], now: float, today: date) -> Decision:
        """Собирать ли группу подписок одного маршрута. Не бросает: любая ошибка — «собираем»."""
        try:
            decision = await self._check(subs, now, today)
        except Exception as e:
            self._warn(f"сбой предфильтра, {_error_text(e)}")
            decision = Decision(True, ERROR)
        self.record(decision.reason, now)
        log.debug("предфильтр подписок %s: %s", [s.id for s in subs], decision.reason)
        return decision

    async def _check(self, subs: Sequence[Subscription], now: float, today: date) -> Decision:
        last_prices: dict[int, int] = {}
        for sub in subs:
            last = self._last_price_in_window(sub, now, today)
            if last is None:
                return Decision(True, FULL)  # страховочный: кэш не спрашиваем
            last_prices[sub.id] = last
        offers = await self._fetch(subs[0])
        if offers is None:
            return Decision(True, ERROR)
        for sub in subs:
            q = sub.query()
            best = tp_preview.cheapest([o for o in offers if o.currency == q.currency], q, sub.criteria)
            cache_price = best.price if best is not None else None
            decision = decide(last_prices[sub.id], cache_price, self.pct, sub.threshold)
            if decision.collect:
                return decision
        return Decision(False, SKIP)

    def _last_price_in_window(self, sub: Subscription, now: float, today: date) -> int | None:
        interval = poll_interval_seconds(sub.depart, today)
        window = safety_window(full_seconds(self.settings, interval), expected_gap(interval, now, sub.last_checked_at))
        if window <= 0 or self.store is None:
            return None
        prices = self.store.recent_prices(sub.id, now - window)
        return prices[-1] if prices else None

    async def _fetch(self, sub: Subscription) -> list[Offer] | None:
        """Кэш по маршруту подписки; None — ошибка, таймаут или лимит (в лог, не чаще раза в 10 минут)."""
        if self._limited_until is not None and self.monotonic() < self._limited_until:
            log.debug("предфильтр подписок: после лимита кэш Aviasales не спрашиваем; собираем на телефонах")
            return None
        try:
            return await asyncio.wait_for(
                travelpayouts.fetch_travelpayouts(
                    sub.query(), self.settings.tp_token, self.settings.tp_marker, limit=tp_preview.PREVIEW_LIMIT
                ),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self._warn(f"не ответил за {self.timeout:.1f} с")
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 429:
                self._limited_until = self.monotonic() + LIMIT_PAUSE_SECONDS
            self._warn(_error_text(e))
        return None

    def _warn(self, reason: str) -> None:
        mono = self.monotonic()
        if self._warned_at is not None and mono - self._warned_at < WARN_EVERY_SECONDS:
            self._quiet_errors += 1
            log.debug("предфильтр подписок: кэш Aviasales — %s; собираем на телефонах", reason)
            return
        more = f" (ещё {self._quiet_errors} за прошлые 10 мин)" if self._quiet_errors else ""
        log.warning(
            "предфильтр подписок: кэш Aviasales — %s; собираем на телефонах, как без предфильтра%s", reason, more
        )
        self._warned_at = mono
        self._quiet_errors = 0

    def record(self, reason: str, ts: float) -> None:
        """Решение в счётчик (в памяти, на 7 суток с часом)."""
        self._events.append((float(ts), reason))
        while self._events and self._events[0][0] < ts - KEEP_SECONDS:
            self._events.popleft()

    def counts(self, since_ts: float) -> dict[str, int]:
        out = dict.fromkeys(REASONS, 0)
        for ts, reason in self._events:
            if ts >= since_ts and reason in out:
                out[reason] += 1
        return out

    def admin_line(self, since_ts: float) -> str:
        """Строка /admin → «Парсинг» под «Источниками»: проверок, пропусков и почему собирали."""
        c = self.counts(since_ts)
        total = sum(c.values())
        line = (
            f"Предфильтр подписок: проверок {total} · пропущено {c[SKIP]} · собрано {total - c[SKIP]} "
            f"(кэш изменился {c[CHANGED]}, у порога {c[THRESHOLD]}, кэш не знает {c[UNKNOWN]}, страховочный {c[FULL]}, "
            f"ошибка кэша {c[ERROR]})"
        )
        if self.started_at > since_ts:
            line += f" · с запуска {datetime.fromtimestamp(self.started_at):%d.%m %H:%M}"
        return line
