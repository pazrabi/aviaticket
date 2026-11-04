from __future__ import annotations

import asyncio
from datetime import date, datetime
from html import escape
import logging
import time
from typing import Any, Callable

from . import advice, analytics
from .filters import apply_criteria
from .formatting import date_ru, offer_card
from .history import PriceHistory
from .models import Offer, Query, SiteResult
from .offers import assign_ids, filter_by_baggage
from .parsing import city_name
from .subscriptions import NotifyRules, Subscription, SubscriptionStore, evaluate

log = logging.getLogger("avia.scheduler")


def build_notification(
    sub: Subscription,
    offer: Offer,
    reasons: list[str],
    now: float | None = None,
    extra_lines: list[str] | None = None,
) -> str:
    """Формирует HTML-текст уведомления об изменении цены по подписке."""
    is_rise = bool(reasons) and all(r.startswith("подорожала") for r in reasons)
    lines: list[str] = ["📈 <b>Цены дорожают</b>" if is_rise else "🔔 <b>Цена изменилась</b>"]

    q = Query(
        origin=sub.origin,
        destination=sub.destination,
        depart=sub.depart,
        adults=sub.adults,
        return_date=sub.return_date,
    )
    dates = date_ru(sub.depart) + (f" – {date_ru(sub.return_date)}" if sub.return_date else "")
    lines.append(f"✈️ <b>{escape(city_name(sub.origin))} → {escape(city_name(sub.destination))}</b>")
    lines.append(f"📅 {dates}  ·  👤 {sub.adults}")
    lines.append("")
    lines.append(f"{'⚠️' if is_rise else '📈'} Причина: {'; '.join(escape(r) for r in reasons)}")
    lines.append("")
    lines.extend(offer_card(offer, q, number=1, baggage_mode=getattr(sub, "baggage_mode", "any"))[1:])

    if sub.criteria:
        desc = sub.criteria.describe()
        if desc:
            lines.append(f"⚙️ {', '.join(escape(d) for d in desc)}")

    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)

    if is_rise:
        lines.append("")
        lines.append("Если планируете лететь, возможно, стоит не откладывать покупку.")

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4096]
    return text


def build_notification_markup(sub: Subscription) -> dict[str, Any]:
    """Формирует инлайн-клавиатуру к уведомлению о цене."""
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Порог цены", "callback_data": f"thr:{sub.id}"},
                {"text": "🔕 Отписаться", "callback_data": f"unsub:{sub.id}"},
            ],
            [{"text": "📊 История", "callback_data": f"hist:sub:{sub.id}"}],
            [{"text": "🔎 Новый поиск", "callback_data": "menu:go"}],
        ]
    }


class Scheduler:
    def __init__(
        self,
        store: SubscriptionStore,
        search_fn: Callable[[Query], Any],
        notify_fn: Callable[[int, str, dict[str, Any] | None], Any],
        history: PriceHistory | None = None,
        rules: NotifyRules | None = None,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
        max_groups_per_run: int = 5,
    ) -> None:
        self.store = store
        self.search_fn = search_fn
        self.notify_fn = notify_fn
        self.history = history
        self.rules = rules if rules is not None else NotifyRules()
        self.clock = clock
        self.today = today
        self.max_groups_per_run = max_groups_per_run

    async def run_once(self) -> int:
        """Один проход планировщика; возвращает число отправленных уведомлений."""
        today_val = self.today()
        now_val = self.clock()
        self.store.expire_past(today_val)
        due = self.store.due(now_val, today_val)
        if not due:
            return 0

        groups: dict[tuple[Any, ...], list[Subscription]] = {}
        for sub in due:
            groups.setdefault(sub.group_key(), []).append(sub)

        # Сортируем группы по давности последней проверки (самые просроченные первыми)
        def _group_staleness(subs: list[Subscription]) -> float:
            checks = [s.last_checked_at for s in subs]
            if any(c is None for c in checks):
                return float("-inf")
            return min(c for c in checks if c is not None)

        sorted_groups = sorted(groups.items(), key=lambda item: _group_staleness(item[1]))
        if self.max_groups_per_run is not None and self.max_groups_per_run > 0:
            sorted_groups = sorted_groups[: self.max_groups_per_run]

        notifications_sent = 0

        for group_key, group_subs in sorted_groups:
            try:
                first_sub = group_subs[0]
                query = first_sub.query()
                try:
                    results = await self.search_fn(query)
                except Exception:
                    log.exception("Ошибка поиска для группы %s", group_key)
                    continue

                assign_ids(results, now=self.clock())
                if self.history is not None:
                    try:
                        await asyncio.to_thread(self.history.annotate, query, results, self.clock())
                    except Exception:
                        log.exception("Ошибка истории цен для группы %s", group_key)

                for sub in group_subs:
                    try:
                        sub_query = sub.query()
                        filtered, _ = apply_criteria(results, sub.criteria)
                        baggage, _ = filter_by_baggage(filtered, sub.baggage_mode)

                        candidates = [
                            o
                            for r in baggage
                            if r.ok
                            for o in r.offers
                            if o.currency == sub_query.currency
                        ]
                        if not candidates:
                            self.store.mark_checked(sub.id, self.clock())
                            continue

                        best = min(candidates, key=lambda o: o.price)

                        now = self.clock()
                        recent_prices: list[int] | None = None
                        if self.history is not None:
                            try:
                                route_prices = await asyncio.to_thread(
                                    self.history.route_search_minima,
                                    sub_query,
                                    now - self.rules.median_window_seconds,
                                    now,
                                )
                                if len(route_prices) >= self.rules.median_min_points:
                                    recent_prices = route_prices
                            except Exception:
                                log.exception(
                                    "Ошибка истории цен маршрута для подписки %s", sub.id
                                )

                        if recent_prices is None:
                            recent_prices = self.store.recent_prices(
                                sub.id, now - self.rules.median_window_seconds
                            )

                        ref = self.store.first_price(sub.id)
                        reasons = evaluate(
                            sub,
                            best.price,
                            best.offer_id,
                            now,
                            recent_prices,
                            self.rules,
                            reference_price=ref,
                        )

                        self.store.record_price(
                            sub.id, best.price, best.currency, best.offer_id, now
                        )
                        self.store.mark_checked(sub.id, now)

                        if reasons:
                            extra_lines: list[str] | None = None
                            if self.history is not None:
                                try:
                                    stats = await asyncio.to_thread(
                                        self.history.route_stats, sub_query
                                    )
                                    site_stats = await asyncio.to_thread(
                                        self.history.cheapest_site_stats, sub_query, now=now
                                    )
                                    extra = analytics.summary_lines(stats, site_stats)[:3]
                                    if extra:
                                        extra_lines = list(extra)

                                    try:
                                        curve = await asyncio.to_thread(self.history.price_curve)
                                    except Exception:
                                        curve = None

                                    try:
                                        dep_val = sub_query.depart
                                        if isinstance(dep_val, datetime):
                                            dep_date = dep_val.date()
                                        elif isinstance(dep_val, date):
                                            dep_date = dep_val
                                        elif isinstance(dep_val, str):
                                            dep_date = date.fromisoformat(dep_val.strip())
                                        else:
                                            dep_date = None
                                        d = (dep_date - today_val).days if dep_date is not None else None
                                        adv = advice.build_advice(stats, d, curve=curve)
                                        if adv is not None:
                                            adv_lines = advice.format_advice(adv)
                                            if adv_lines:
                                                advice_line = f"{adv_lines[0]} — {escape(adv.text.rstrip('.'))}"
                                                if extra_lines is None:
                                                    extra_lines = [advice_line]
                                                else:
                                                    extra_lines.append(advice_line)
                                    except Exception:
                                        log.exception(
                                            "Ошибка расчёта совета для подписки %s", sub.id
                                        )
                                except Exception:
                                    log.exception(
                                        "Ошибка расчёта аналитики для подписки %s", sub.id
                                    )
                                    extra_lines = None

                            text = build_notification(
                                sub, best, reasons, now=now, extra_lines=extra_lines
                            )
                            markup = build_notification_markup(sub)
                            try:
                                await self.notify_fn(sub.chat_id, text, markup)
                                self.store.record_notification(
                                    sub.id, best.offer_id, best.price, reasons, now
                                )
                                notifications_sent += 1
                            except Exception:
                                log.exception(
                                    "Ошибка отправки уведомления для подписки %s", sub.id
                                )
                    except Exception:
                        log.exception("Ошибка обработки подписки %s", sub.id)
            except Exception:
                log.exception("Ошибка обработки группы %s", group_key)

        return notifications_sent

    async def run_forever(self, tick_seconds: float = 300.0) -> None:
        """Бесконечный цикл run_once; ошибки логируются, asyncio.CancelledError пробрасывается."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка во время прохода планировщика")
            await asyncio.sleep(tick_seconds)


__all__ = ["Scheduler", "build_notification", "build_notification_markup"]
