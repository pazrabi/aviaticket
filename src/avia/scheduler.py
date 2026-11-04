from __future__ import annotations

import asyncio
from datetime import date
from html import escape
import logging
import time
from typing import Any, Callable

from .filters import apply_criteria
from .formatting import offer_line, offer_link_caption
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
) -> str:
    """Формирует HTML-текст уведомления об изменении цены по подписке."""
    is_rise = bool(reasons) and all(r.startswith("подорожала") for r in reasons)
    head = "📈 <b>Цены дорожают</b>" if is_rise else "🔔 <b>Цена изменилась</b>"
    lines: list[str] = [head]

    orig_name = escape(city_name(sub.origin))
    dest_name = escape(city_name(sub.destination))
    if sub.return_date:
        dates = f"{sub.depart:%d.%m}–{sub.return_date:%d.%m} (туда-обратно)"
    else:
        dates = f"{sub.depart:%d.%m} (в одну сторону)"
    lines.append(f"{orig_name} → {dest_name}, {dates}, пассажиров: {sub.adults}")

    reasons_str = "; ".join(escape(r) for r in reasons)
    reason_prefix = "⚠️ Причина:" if is_rise else "Причина:"
    lines.append(f"{reason_prefix} {reasons_str}")

    offer_str = offer_line(offer, baggage_mode=getattr(sub, "baggage_mode", "any"))
    site_html = f"<i>{escape(offer.site)}</i>"
    if offer.url:
        caption = offer_link_caption(offer)
        link_html = f'<a href="{escape(offer.url, quote=True)}">{caption}</a>'
        site_part = f"{site_html} {link_html}"
    else:
        site_part = site_html

    lines.append(f"{offer_str} · {site_part}")

    if is_rise:
        lines.append("Если планируете лететь, возможно, стоит не откладывать покупку.")

    if sub.criteria:
        desc = sub.criteria.describe()
        if desc:
            lines.append(f"Фильтры: {', '.join(escape(d) for d in desc)}")

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4096]
    return text


def build_notification_markup(sub: Subscription) -> dict[str, Any]:
    """Формирует инлайн-клавиатуру к уведомлению о цене."""
    return {
        "inline_keyboard": [
            [{"text": "🔕 Отписаться", "callback_data": f"unsub:{sub.id}"}],
            [{"text": "🎯 Порог цены", "callback_data": f"thr:{sub.id}"}],
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
    ) -> None:
        self.store = store
        self.search_fn = search_fn
        self.notify_fn = notify_fn
        self.history = history
        self.rules = rules if rules is not None else NotifyRules()
        self.clock = clock
        self.today = today

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

        notifications_sent = 0

        for group_key, group_subs in groups.items():
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
                        recent = self.store.recent_prices(
                            sub.id, now - self.rules.median_window_seconds
                        )
                        ref = self.store.first_price(sub.id)
                        reasons = evaluate(
                            sub,
                            best.price,
                            best.offer_id,
                            now,
                            recent,
                            self.rules,
                            reference_price=ref,
                        )

                        self.store.record_price(
                            sub.id, best.price, best.currency, best.offer_id, now
                        )
                        self.store.mark_checked(sub.id, now)

                        if reasons:
                            text = build_notification(sub, best, reasons, now=now)
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
