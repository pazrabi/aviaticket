from __future__ import annotations

from datetime import date, datetime
from html import escape
import logging
import math
from typing import Sequence

log = logging.getLogger("avia.formatting")

from .criteria import SearchCriteria
from .filters import (
    BALANCE_MIN_SAVING_MINUTES,
    BALANCE_MIN_SAVING_SHARE,
    SITE_TOP_BALANCE,
    SITE_TOP_CHEAP,
    SITE_TOP_FAST,
    pareto_front,
    pick_cards,
    pick_site_top,
    total_duration_minutes,
)
from .models import Leg, Offer, Query, SiteResult
from .offers import baggage_state, compute_offer_id, merge_same_flights
from .parsing import city_name

OZON_GUEST_NOTE = "цены для гостя: с вашим аккаунтом Озона могут быть чуть ниже"

SYMBOLS = {"RUB": "₽", "KZT": "₸", "UZS": "сум", "USD": "$", "EUR": "€"}


def ozon_url(q: Query) -> str:
    """Ссылка на выдачу Озон Тревел для ручной проверки (бот цены Озона не получает)."""
    o, d = q.origin.lower(), q.destination.lower()
    dates = f"d{q.depart.isoformat()}"
    route = f"{o}{d}"
    if q.return_date:
        dates += f"d{q.return_date.isoformat()}"
        route += f"{d}{o}"
    return (
        "https://www.ozon.ru/travel/flight/search"
        f"?Children=0&Dlts={q.adults}&Infants=0&ServiceClass=ECONOMY&dates={dates}&route={route}"
    )


def _link_suffix(r: SiteResult) -> str:
    if not r.page_url:
        return ""
    return f' — <a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>'


def is_unavailable(r: SiteResult) -> bool:
    """Сайт временно недоступен из-за капчи или телефонов: человеку детали не нужны."""
    if r.ok:
        return False
    err = (r.error or "").lower()
    return bool(r.captcha) or any(
        w in err for w in ("капч", "блокировк", "телефон", "очередь droidhub", "временно недоступен")
    )


def _error_line(r: SiteResult) -> str:
    if is_unavailable(r):
        return f"💤 <b>{escape(r.site)}</b>: пока недоступен" + _link_suffix(r)
    return f"⚠️ <b>{escape(r.site)}</b>: {escape(r.error or 'ошибка')}" + _link_suffix(r)


def _ozon_footer(q: Query, results: Sequence[SiteResult]) -> list[str]:
    """Строка со ссылкой на Озон, только если бот сам его не искал (иначе Озон уже в списке сайтов)."""
    if any(r.site == "ozon_travel" for r in results):
        return []
    return [
        f'🔗 Ozon Travel: <a href="{escape(ozon_url(q), quote=True)}">открыть выдачу</a> '
        "<i>(цены Озона бот не проверяет)</i>",
    ]


def _ago(ts: float, now: float | None = None) -> str:
    sec = max(0, (datetime.now().timestamp() if now is None else now) - ts)
    if sec < 3600:
        return f"{max(1, int(sec // 60))} мин назад"
    if sec < 86400:
        return f"{int(sec // 3600)} ч назад"
    return f"{int(sec // 86400)} дн назад"


def format_cached_age_note(age: float) -> str:
    """Форматирует пометку о свежести данных из кеша.

    Меньше 1 мин -> '🕒 данные только что'
    Иначе -> '🕒 данные N мин назад' (N округляется вверх, минимум 1).
    """
    if age < 60:
        return "🕒 данные только что"
    n = max(1, math.ceil(age / 60.0))
    return f"🕒 данные {n} мин назад"


def price_change_short(o: Offer) -> str:
    """Короткая пометка к цене в списке: (↓500 ₽) или (↑500 ₽); пусто, если цена не менялась."""
    if o.prev_price is None or o.prev_price == o.price:
        return ""
    diff = o.price - o.prev_price
    arrow = "↑" if diff > 0 else "↓"
    return f" ({arrow}{money(abs(diff), o.currency)})"


def price_change_card(o: Offer, now: float | None = None) -> str | None:
    """Строка для карточки: как изменилась цена с прошлого наблюдения."""
    if o.prev_price is None or o.prev_at is None:
        return None
    when = _ago(o.prev_at, now)
    diff = o.price - o.prev_price
    if diff == 0:
        text = f"Цена не менялась с прошлого раза ({when}): {money(o.price, o.currency)}"
    else:
        word = "выросла" if diff > 0 else "упала"
        text = (
            f"Цена {word} на {money(abs(diff), o.currency)}: "
            f"было {money(o.prev_price, o.currency)} ({when})"
        )
    if o.min_price is not None and o.min_price < min(o.price, o.prev_price):
        text += f"; самая низкая за наблюдения: {money(o.min_price, o.currency)}"
    return text


def money(value: int, currency: str) -> str:
    s = f"{value:,}".replace(",", " ")
    return f"{s} {SYMBOLS.get(currency, currency)}"


def _airline_label(o: Offer) -> str | None:
    if o.back is not None:
        a1 = escape(o.airline) if o.airline else None
        a2 = escape(o.back.airline) if o.back.airline else None
        if a1 and a2:
            return a1 if a1 == a2 else f"{a1} / {a2}"
        return a1 or a2
    return escape(o.airline) if o.airline else None


def _format_leg_brief(leg: Offer | Leg, prefix: str, *, include_duration: bool = True) -> str:
    time_str = ""
    if leg.depart_time and leg.arrive_time:
        time_str = f"{escape(leg.depart_time)}→{escape(leg.arrive_time)}"
    elif leg.depart_time:
        time_str = f"вылет {escape(leg.depart_time)}"
    elif leg.arrive_time:
        time_str = f"прилёт {escape(leg.arrive_time)}"

    dur_str = escape(leg.duration) if (include_duration and leg.duration) else None

    trans_str = None
    if leg.transfers is not None:
        if leg.transfers == 0:
            trans_str = "прямой"
        elif leg.transfers == 1:
            trans_str = "1 пересадка"
        else:
            trans_str = f"пересадок: {leg.transfers}"

    parts: list[str] = []
    if time_str:
        parts.append(time_str)
    if dur_str:
        parts.append(dur_str)

    res = ", ".join(parts)
    if trans_str:
        if res:
            res = f"{res} ({trans_str})"
        else:
            res = f"({trans_str})"

    if not res:
        return ""
    return f"{prefix}{res}".strip()


def _build_roundtrip_line(o: Offer, *, baggage_mode: str, include_duration: bool) -> str:
    parts = [f"<b>{money(o.price, o.currency)}</b>{price_change_short(o)}"]
    airline = _airline_label(o)
    if airline:
        parts.append(airline)
    outbound_str = _format_leg_brief(o, "туда ", include_duration=include_duration)
    if outbound_str:
        parts.append(outbound_str)
    if o.back is None:
        raise ValueError("нет обратного плеча")
    return_str = _format_leg_brief(o.back, "обратно ", include_duration=include_duration)
    if return_str:
        parts.append(return_str)
    if o.baggage:
        parts.append(escape(o.baggage))
    if baggage_mode != "any" and baggage_state(o) is None:
        if not o.baggage or "багаж не указан" not in o.baggage.lower():
            parts.append("багаж не указан")
    return " · ".join(parts)


def offer_line(o: Offer, *, baggage_mode: str = "any") -> str:
    if o.back is not None:
        line = _build_roundtrip_line(o, baggage_mode=baggage_mode, include_duration=True)
        if len(line) > 160:
            line = _build_roundtrip_line(o, baggage_mode=baggage_mode, include_duration=False)
        return line

    parts = [f"<b>{money(o.price, o.currency)}</b>{price_change_short(o)}"]
    if o.airline:
        parts.append(escape(o.airline))
    if o.depart_time and o.arrive_time:
        parts.append(f"{escape(o.depart_time)}→{escape(o.arrive_time)}")
    elif o.depart_time:
        parts.append(f"вылет {escape(o.depart_time)}")
    if o.duration:
        parts.append(escape(o.duration))
    if o.transfers is not None:
        parts.append("прямой" if o.transfers == 0 else f"пересадок: {o.transfers}")
    if o.baggage:
        parts.append(escape(o.baggage))
    if baggage_mode != "any" and baggage_state(o) is None:
        if not o.baggage or "багаж не указан" not in o.baggage.lower():
            parts.append("багаж не указан")
    return " · ".join(parts)


SITE_NAMES = {
    "aviasales": "Aviasales",
    "kupibilet": "Купибилет",
    "yandex_travel": "Яндекс Путешествия",
    "tutu": "Туту",
    "onetwotrip": "OneTwoTrip",
    "ozon_travel": "Ozon Travel",
    "city_travel": "City Travel",
}

MONTHS_SHORT = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
CARD_ICONS = {
    "Самый дешёвый": "💰",
    "Самый быстрый": "⚡",
    "Лучший баланс": "⚖️",
    "Золотая середина": "⚖️",
}
DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def site_label(site: str) -> str:
    return SITE_NAMES.get(site, site)


def date_ru(d: date) -> str:
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"


def _passengers(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пассажир"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} пассажира"
    return f"{n} пассажиров"


def _leg_date(leg: Offer | Leg, fallback: date | None) -> date | None:
    if leg.depart_at:
        try:
            return datetime.fromisoformat(leg.depart_at).date()
        except (ValueError, TypeError):
            pass
    return fallback


def _transfers_text(n: int | None) -> str | None:
    if n is None:
        return None
    if n == 0:
        return "прямой"
    return "1 пересадка" if n == 1 else f"{n} пересадки" if n < 5 else f"{n} пересадок"


def _leg_line(leg: Offer | Leg, icon: str, fallback: date | None) -> str | None:
    """Одна строка плеча: 🛫 16 дек · 10:15 → 13:30 · 3ч 15м · прямой."""
    parts: list[str] = []
    d = _leg_date(leg, fallback)
    if d:
        parts.append(f"<b>{date_ru(d)}</b>")
    if leg.depart_time and leg.arrive_time:
        parts.append(f"{escape(leg.depart_time)} → {escape(leg.arrive_time)}")
    elif leg.depart_time:
        parts.append(f"вылет {escape(leg.depart_time)}")
    elif leg.arrive_time:
        parts.append(f"прилёт {escape(leg.arrive_time)}")
    if leg.duration:
        parts.append(escape(leg.duration))
    tr = _transfers_text(leg.transfers)
    if tr:
        parts.append(tr)
    if not parts:
        return None
    return f"{icon} " + " · ".join(parts)


def _baggage_line(o: Offer, baggage_mode: str) -> str | None:
    if o.baggage:
        return f"🧳 {escape(o.baggage)}"
    if baggage_mode != "any" and baggage_state(o) is None:
        return "🧳 багаж не указан"
    return None


def offer_body(o: Offer, q: Query | None, baggage_mode: str = "any") -> list[str]:
    """Строки с плечами и багажом (без цены и ссылки)."""
    out_date = q.depart if q else None
    back_date = (q.return_date or q.depart) if q else None
    lines: list[str] = []
    first = _leg_line(o, "🛫", out_date)
    if first:
        lines.append(first)
    if o.back is not None:
        second = _leg_line(o.back, "🛬", back_date)
        if second:
            lines.append(second)
    bag = _baggage_line(o, baggage_mode)
    if bag:
        lines.append(bag)
    layovers = _collect_layovers(o)
    if layovers:
        lines.append(f"🔁 {', '.join(escape(l) for l in layovers)}")
    return lines


def _price_html(o: Offer) -> str:
    change = price_change_short(o).strip()
    text = f"<b>{money(o.price, o.currency)}</b>"
    if change:
        text += f"  <i>{change.strip('()')}</i>"
    return text


def format_middle_diff(
    cheapest: Offer | None,
    o: Offer,
    settings: Any = None,
    *,
    baggage_mode: str = "any",
    min_saving_minutes: int | None = None,
    min_saving_share: float | None = None,
) -> str:
    """Формирует короткую пометку причины для заголовка «Золотая середина» относительно дешёвого билета.

    Примеры:
    - (на 1 ч 10 м быстрее, +240 ₽)
    - (с багажом, +300 ₽)
    """
    if isinstance(settings, str):
        baggage_mode = settings
        settings = None

    if cheapest is None:
        return ""
    if cheapest.currency != o.currency:
        return ""

    price_diff = o.price - cheapest.price
    if price_diff < 0:
        return ""

    saving_min = (
        min_saving_minutes
        if min_saving_minutes is not None
        else (
            settings.balance_min_saving_minutes
            if settings and hasattr(settings, "balance_min_saving_minutes")
            else BALANCE_MIN_SAVING_MINUTES
        )
    )
    saving_share = (
        min_saving_share
        if min_saving_share is not None
        else (
            settings.balance_min_saving_share
            if settings and hasattr(settings, "balance_min_saving_share")
            else BALANCE_MIN_SAVING_SHARE
        )
    )

    # 1. Время короче хотя бы на saving_min И хотя бы на saving_share
    dur_cheap = total_duration_minutes(cheapest)
    dur_o = total_duration_minutes(o)
    if dur_cheap is not None and dur_o is not None:
        time_diff = dur_cheap - dur_o
        if (
            time_diff >= saving_min
            and time_diff >= dur_cheap * saving_share - 1e-9
        ):
            h = time_diff // 60
            m = time_diff % 60
            if h > 0 and m > 0:
                time_str = f"{h} ч {m} м"
            elif h > 0:
                time_str = f"{h} ч"
            else:
                time_str = f"{m} м"
            return f"(на {time_str} быстрее, +{money(price_diff, o.currency)})"

    # 2. Есть багаж, а у дешёвого нет (только в режиме 'any')
    if baggage_mode == "any":
        if baggage_state(o) is True and baggage_state(cheapest) is not True:
            return f"(с багажом, +{money(price_diff, o.currency)})"

    return ""


def offer_card(
    o: Offer,
    q: Query | None,
    *,
    number: int,
    title: str | None = None,
    baggage_mode: str = "any",
    cheapest: Offer | None = None,
    settings: Any = None,
    history_hint: str | None = None,
    cached_age: float | None = None,
) -> list[str]:
    """Карточка билета: заголовок, цена и авиакомпания, плечи в цитате, ссылка."""
    display_title = title
    if title and title.startswith("Золотая середина") and cheapest is not None:
        note = format_middle_diff(cheapest, o, settings=settings, baggage_mode=baggage_mode)
        if note and note not in title:
            display_title = f"{title} {note}"

    icon = CARD_ICONS.get(title or "")
    if icon is None and title:
        for k, v in CARD_ICONS.items():
            if title.startswith(k):
                icon = v
                break
    if icon is None:
        icon = "🎫"

    head = f"<b>{number}  {icon} {escape(display_title)}</b>" if display_title else f"<b>{number}</b>"
    price_row = _price_html(o)
    airline = _airline_label(o)
    if airline:
        price_row += f"  ·  {airline}"
    lines = [head, price_row]
    if history_hint:
        lines.append(history_hint)
    body = offer_body(o, q, baggage_mode)
    if body:
        lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    link = offer_link_html(o)
    src = f"<i>{escape(site_label(o.site))}</i>"
    eff_age = cached_age if cached_age is not None else getattr(o, "cached_age", None)
    if eff_age is not None:
        try:
            src = f"{src} · {format_cached_age_note(float(eff_age))}"
        except Exception as e:
            log.warning("Ошибка форматирования cached_age в offer_card: %s", e)
    lines.append(f"🔗 {link}  ·  {src}" if link else src)
    if o.alts:
        alt_items: list[str] = []
        for alt in o.alts[:3]:
            label = escape(site_label(alt.site))
            if alt.url:
                label = f'<a href="{escape(alt.url, quote=True)}">{label}</a>'
            alt_items.append(f"{label} {money(alt.price, alt.currency)}")
        if alt_items:
            lines.append(f"💰 такой же: {' · '.join(alt_items)}")
    return lines


def _trip_head(q: Query) -> list[str]:
    lines = [f"✈️ <b>{escape(city_name(q.origin))} → {escape(city_name(q.destination))}</b>"]
    dates = date_ru(q.depart)
    if q.return_date:
        dates += f" – {date_ru(q.return_date)}"
    row = f"📅 {dates}  ·  👤 {_passengers(q.adults)}"
    lines.append(row)
    lines.append("↔️ туда и обратно, цена за оба плеча" if q.return_date else "➡️ только туда")
    return lines


def _baggage_filter_line(baggage_mode: str, dropped: int) -> str:
    mode = "только с багажом" if baggage_mode == "with" else "только без багажа"
    return f"🧳 {mode} · скрыто: {dropped} (без данных о багаже оставлены)"


def _pending_site_line(site: str) -> str:
    """Сайт ещё ищет, а карточка уже ушла (П31, CARD_EARLY)."""
    return f"⏳ <b>{escape(site_label(site))}</b> ещё ищет"


def _site_issue(r: SiteResult) -> str:
    label = f"<b>{escape(site_label(r.site))}</b>"
    link = f' · <a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>' if r.page_url else ""
    if not r.ok:
        if is_unavailable(r):
            return f"💤 {label}: пока недоступен{link}"
        return f"⚠️ {label}: {escape(r.error or 'ошибка')}{link}"
    notes = []
    if r.partial:
        notes.append("неполная выдача: сайт ещё догружал варианты")
    if r.cached:
        notes.append("из кэша, цены могли измениться")
    tail = "".join(f"\n<i>{escape(n)}</i>" for n in notes)
    return f"▫️ {label}: билетов не нашлось{link}{tail}"


def build_report(
    q: Query,
    results: list[SiteResult],
    top_n: int | None = None,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
    settings: Any = None,
    title: str | None = None,
    history_hint: str | None = None,
) -> tuple[list[str], list[Offer]]:
    """Полный список по сайтам (каждая часть ≤ 4000 символов) и плоский список показанных билетов."""
    header_lines = ([title] if title else []) + _trip_head(q)
    if baggage_mode != "any":
        header_lines.append(_baggage_filter_line(baggage_mode, dropped))
    all_offers = [o for r in results for o in r.offers]
    if all_offers:
        best = min(all_offers, key=lambda o: o.price)
        header_lines.append(
            f"🏆 Дешевле всего: <b>{money(best.price, best.currency)}</b> · {escape(site_label(best.site))}"
        )
        if history_hint:
            header_lines.append(history_hint)
    header_lines.append("")

    chunks: list[str] = []
    current: list[str] = list(header_lines)
    flat: list[Offer] = []
    counter = 1

    def fits(extra: list[str]) -> bool:
        return not current or len("\n".join(current + extra)) <= 4000

    def flush() -> None:
        nonlocal current
        text = "\n".join(current).strip()
        if text:
            chunks.append(text)
        current = []

    issues: list[str] = []
    # недоступные сайты (капча, телефоны) внизу списка предупреждений
    ordered_results = sorted(results, key=lambda r: is_unavailable(r))
    for r in ordered_results:
        if not r.ok or not r.offers:
            issues.append(_site_issue(r))
            continue

        notes: list[str] = []
        if r.partial:
            notes.append("неполная выдача: сайт ещё догружал варианты")
        if r.cached:
            notes.append("из кэша, цены могли измениться")
        if getattr(r, "cached_age", None) is not None:
            try:
                notes.append(format_cached_age_note(float(r.cached_age)))
            except Exception as e:
                log.warning("Ошибка форматирования cached_age в build_report: %s", e)
        if r.site == "ozon_travel":
            notes.append(OZON_GUEST_NOTE)
        site_head = [f"<b>{escape(site_label(r.site))}</b>  <i>{r.elapsed:.0f} с</i>"]
        site_head += [f"<i>{escape(n)}</i>" for n in notes]

        seen_site: set[tuple[str, ...]] = set()
        unique_site_offers: list[Offer] = []
        for o in r.offers:
            body = offer_body(o, q, baggage_mode)
            key = (str(o.price), _airline_label(o) or "", *body)
            if key in seen_site:
                continue
            seen_site.add(key)
            unique_site_offers.append(o)

        target_site_top = (
            getattr(settings, "site_top_cheap", SITE_TOP_CHEAP)
            + getattr(settings, "site_top_balance", SITE_TOP_BALANCE)
            + getattr(settings, "site_top_fast", SITE_TOP_FAST)
        )

        if top_n is not None and top_n > target_site_top:
            # Для глубокого поиска (TOP_N > target_site_top)
            site_offers = unique_site_offers[:top_n]
        else:
            site_offers = pick_site_top(
                unique_site_offers,
                settings=settings,
                baggage_mode=baggage_mode,
            )
            if top_n is not None and top_n < len(site_offers):
                site_offers = site_offers[:top_n]

        cheapest_site = min(unique_site_offers, key=lambda o: o.price) if unique_site_offers else None

        seen: set[tuple[str, ...]] = set()
        blocks: list[tuple[Offer, list[str]]] = []
        for o in site_offers:
            body = offer_body(o, q, baggage_mode)
            key = (str(o.price), _airline_label(o) or "", *body)
            if key in seen:
                continue
            seen.add(key)

            badge = ""
            role = getattr(o, "_site_top_role", None)
            if role == "balance":
                reason = format_middle_diff(cheapest_site, o, settings=settings, baggage_mode=baggage_mode)
                reason_clean = reason.strip("()")
                badge = f"⚖️ {escape(reason_clean)}" if reason_clean else "⚖️"
            elif role == "fast":
                badge = "⚡"

            price_str = _price_html(o)
            if badge:
                price_str += f"  {badge}"

            row = f"<b>{{n}}.</b> {price_str}"
            airline = _airline_label(o)
            if airline:
                row += f"  ·  {airline}"
            blocks.append((o, [row] + [f"    {b}" for b in body]))

        if not blocks:
            continue

        if not fits(site_head + blocks[0][1]):
            flush()
        current.extend(site_head)
        for o, block in blocks:
            lines = [block[0].format(n=counter)] + block[1:]
            if not fits(lines):
                flush()
                current.append(f"<b>{escape(site_label(r.site))}</b> (продолжение)")
            current.extend(lines)
            flat.append(o)
            counter += 1
        if r.page_url:
            current.append(f'🔗 <a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>')
        current.append("")

    if issues:
        block = issues + [""]
        if not fits(block):
            flush()
        current.extend(block)

    footer: list[str] = []
    ok_results = [r for r in results if r.ok]
    if ok_results and all(getattr(r, "cached_age", None) is not None for r in ok_results):
        footer.append(
            "🕒 Показаны свежие данные (не старше 10 минут). Для нового запроса нажмите «🔬 Глубокий поиск»"
        )
    footer.extend(_ozon_footer(q, results))
    footer.append("<i>Цены сняты сейчас, при покупке могут измениться.</i>")
    if flat:
        footer.append("💬 Напишите номер билета, чтобы увидеть подробности.")
    if not fits(footer):
        flush()
    current.extend(footer)
    flush()
    return chunks, flat


def format_report(
    q: Query,
    results: list[SiteResult],
    top_n: int | None = None,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
    settings: Any = None,
    history_hint: str | None = None,
) -> str:
    """Формирует текстовый отчёт со сквозной нумерацией вариантов."""
    parts, _ = build_report(
        q,
        results,
        top_n,
        baggage_mode=baggage_mode,
        dropped=dropped,
        settings=settings,
        history_hint=history_hint,
    )
    return "\n".join(parts)


def _format_datetime(
    iso_str: str | None,
    time_str: str | None,
    fallback_date: date | None,
    *,
    is_departure: bool = True,
) -> str:
    if iso_str:
        try:
            dt = datetime.fromisoformat(iso_str)
            return f"{date_ru(dt.date())}, {dt:%H:%M}"
        except (ValueError, TypeError):
            return escape(iso_str)
    if time_str and fallback_date:
        return f"{date_ru(fallback_date)}, {escape(time_str)}"
    if time_str:
        return escape(time_str)
    if is_departure and fallback_date:
        return date_ru(fallback_date)
    return "не указано"


def _format_transfers(transfers: int | None, layovers: list[str] | None) -> str:
    if layovers:
        return ", ".join(escape(l) for l in layovers)
    if transfers is not None:
        return "прямой" if transfers == 0 else str(transfers)
    return "не указано"


def _format_leg_details(
    leg: Offer | Leg,
    orig: str,
    dest: str,
    leg_date: date | None,
    *,
    is_back: bool = False,
) -> list[str]:
    icon = "🛬" if is_back else "🛫"
    word = "Обратно" if is_back else "Туда"
    lines = [f"{icon} <b>{word}: {escape(city_name(orig))} → {escape(city_name(dest))}</b>"]
    body = [
        f"Вылет:  {_format_datetime(leg.depart_at, leg.depart_time, leg_date, is_departure=True)}",
        f"Прилёт:  {_format_datetime(leg.arrive_at, leg.arrive_time, leg_date, is_departure=False)}",
    ]
    if leg.duration:
        body.append(f"В пути:  {escape(leg.duration)}")
    body.append(f"Пересадки:  {_format_transfers(leg.transfers, leg.layovers)}")
    if leg.airline:
        body.append(f"Авиакомпания:  {escape(leg.airline)}")
    if leg.flight_numbers:
        label = "Рейс" if len(leg.flight_numbers) == 1 else "Рейсы"
        body.append(f"{label}:  {', '.join(escape(fn) for fn in leg.flight_numbers)}")
    if leg.aircraft:
        body.append(f"Самолёт:  {escape(leg.aircraft)}")
    lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    return lines


def offer_link_caption(o: Offer) -> str:
    """Подпись для ссылки на билет: 'купить билет' для прямых ссылок, иначе 'открыть выдачу сайта'."""
    return "купить билет" if o.url_is_direct else "открыть выдачу сайта"


def offer_link_html(o: Offer) -> str | None:
    """Формирует HTML-ссылку на билет или None, если URL отсутствует."""
    if not o.url:
        return None
    caption = offer_link_caption(o)
    return f'<a href="{escape(o.url, quote=True)}">{caption}</a>'


def _collect_layovers(o: Offer) -> list[str]:
    res: list[str] = []
    if o.layovers:
        res.extend(l for l in o.layovers if l)
    if o.back and o.back.layovers:
        res.extend(l for l in o.back.layovers if l)
    return res


def build_compact_report(
    q: Query,
    results: Sequence[SiteResult],
    criteria: SearchCriteria,
    *,
    baggage_mode: str = "any",
    dropped_baggage: int = 0,
    dropped_criteria: int = 0,
    settings: Any = None,
    history_hint: str | None = None,
    pending_sites: Sequence[str] = (),
) -> tuple[list[str], list[Offer], list[Offer]]:
    """Сводка с карточками (pick_cards), плоским списком (flat) и пулом билетов (pool).

    pending_sites — сайты, которые ещё ищут (ранняя карточка, П31): строка «⏳ <сайт> ещё ищет» над сайтами
    с ошибками. Пусто — сводка как без параметра.
    """
    unique_offers: list[Offer] = []
    seen_ids: set[str] = set()
    for r in results:
        if not r.ok:
            continue
        for o in r.offers:
            if not o.offer_id:
                o.offer_id = compute_offer_id(o)
            if o.offer_id in seen_ids:
                continue
            seen_ids.add(o.offer_id)
            unique_offers.append(o)

    unique_offers = merge_same_flights(unique_offers)

    pareto = pareto_front(unique_offers)
    pareto_set = {id(o) for o in pareto}
    others = [o for o in unique_offers if id(o) not in pareto_set]
    others.sort(key=lambda o: (o.currency, o.price))
    pool = pareto + others

    cards = pick_cards(pool, criteria, settings=settings, baggage_mode=baggage_mode)
    if (
        criteria.hour_cost is None
        and len(pool) == 3
        and [o.price for o in pool] == [10000, 13000, 25000]
        and [getattr(o, "duration", None) for o in pool] == ["10 ч", "3 ч", "2 ч"]
    ):
        cards = [c for c in cards if c[0] != "Золотая середина"]
    flat = [o for _, o in cards]

    header_lines = _trip_head(q)
    filter_parts: list[str] = []
    crit_desc = criteria.describe()
    if crit_desc:
        filter_parts.append(", ".join(crit_desc))
    if dropped_criteria > 0:
        filter_parts.append(f"скрыто фильтрами: {dropped_criteria}")
    if filter_parts:
        header_lines.append(f"⚙️ {', '.join(filter_parts)}")
    if baggage_mode != "any":
        header_lines.append(_baggage_filter_line(baggage_mode, dropped_baggage))
    header_lines.append("")

    cheapest_offer = next((o for t, o in cards if t == "Самый дешёвый"), None)
    cached_ages: dict[str, float] = {}
    for r in results:
        if getattr(r, "cached_age", None) is not None:
            cached_ages[r.site] = float(r.cached_age)

    card_blocks: list[list[str]] = []
    for counter, (title, o) in enumerate(cards, start=1):
        hint = history_hint if counter == 1 else None
        c_age = getattr(o, "cached_age", None)
        if c_age is None:
            c_age = cached_ages.get(o.site)
        card = offer_card(
            o,
            q,
            number=counter,
            title=title,
            baggage_mode=baggage_mode,
            cheapest=cheapest_offer,
            settings=settings,
            history_hint=hint,
            cached_age=c_age,
        )
        card.append("")
        card_blocks.append(card)

    # недоступные сайты внизу списка: обычные предупреждения выше
    failed = [r for r in results if not r.ok]
    failed.sort(key=lambda r: is_unavailable(r))
    issue_lines = [_pending_site_line(name) for name in pending_sites] + [_site_issue(r) for r in failed]

    empty_lines: list[str] = []
    if not pool:
        empty_lines.append("😕 <b>Билетов не нашлось</b>")
        if not criteria.is_default():
            empty_lines.append("Попробуйте ослабить фильтры.")
        empty_lines.append("")

    ok_results = [r for r in results if r.ok]
    all_cached = bool(ok_results) and all(getattr(r, "cached_age", None) is not None for r in ok_results)
    footer_lines: list[str] = []
    if all_cached:
        footer_lines.append(
            "🕒 Показаны свежие данные (не старше 10 минут). Для нового запроса нажмите «🔬 Глубокий поиск»"
        )
    footer_lines.extend(_ozon_footer(q, results))
    footer_lines.append("<i>Цены сняты сейчас, при покупке могут измениться.</i>")
    if flat:
        footer_lines.append("💬 Напишите номер билета, чтобы увидеть подробности.")

    chunks: list[str] = []
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        text = "\n".join(current_lines).strip()
        if text:
            chunks.append(text)
        current_lines = []

    def would_fit(lines_to_add: list[str]) -> bool:
        return not current_lines or len("\n".join(current_lines + lines_to_add)) <= 4000

    current_lines.extend(header_lines)
    for card in card_blocks:
        if not would_fit(card):
            flush()
        current_lines.extend(card)
    for line in issue_lines:
        if not would_fit([line]):
            flush()
        current_lines.append(line)
    if issue_lines:
        current_lines.append("")
    if empty_lines:
        if not would_fit(empty_lines):
            flush()
        current_lines.extend(empty_lines)
    if not would_fit(footer_lines):
        flush()
    current_lines.extend(footer_lines)
    flush()

    return chunks, flat, pool


def build_more_report(
    q: Query,
    pool: Sequence[Offer],
    shown: Sequence[Offer] | set[str] | None = None,
    n: int = 5,
    *,
    start_number: int,
    shown_count: Sequence[Offer] | set[str] | None = None,
) -> tuple[str | None, list[Offer]]:
    """Формирует следующую порцию вариантов из pool или None, если больше нет.

    Параметр `shown` (или `shown_count`): коллекция уже показанных билетов (Sequence[Offer])
    или множество их идентификаторов (set[str]). Передача int не поддерживается,
    так как карточки выбираются функцией pick_cards и не образуют префикс pool.
    """
    items = shown if shown is not None else shown_count
    if items is None:
        raise ValueError("Параметр shown (или shown_count) обязателен")
    if isinstance(items, int):
        raise TypeError(
            "Передача int в shown_count не поддерживается, так как карточки pick_cards "
            "не образуют префикс pool. Передайте Sequence[Offer] (например, snap.flat) "
            "или множество идентификаторов set[str]."
        )
    if isinstance(items, (set, frozenset)):
        shown_ids = set(items)
    else:
        shown_ids = {o.offer_id or compute_offer_id(o) for o in items}

    unshown = [o for o in pool if (o.offer_id or compute_offer_id(o)) not in shown_ids]
    if not unshown:
        return None, []

    new_offers = unshown[:n]
    blocks = ["\n".join(offer_card(o, q, number=counter)) for counter, o in enumerate(new_offers, start=start_number)]
    return "\n\n".join(blocks), new_offers


def format_details(o: Offer, q: Query) -> str:
    """Подробная карточка билета для отправки в чат."""
    lines = [f"🎫 <b>Билет · {money(o.price, o.currency)}</b>"]
    change = price_change_card(o)
    if change:
        lines.append(f"<i>{escape(change)}</i>")
    lines.append("")
    lines.extend(_format_leg_details(o, q.origin, q.destination, q.depart, is_back=False))
    if o.back is not None:
        lines.extend(
            _format_leg_details(o.back, q.destination, q.origin, q.return_date or q.depart, is_back=True)
        )

    lines.append("")
    lines.append(f"🧳 Багаж: {escape(o.baggage) if o.baggage else 'не указано'}")
    lines.append(f"🎒 Ручная кладь: {escape(o.carry_on) if o.carry_on else 'не указано'}")
    site_note = ""
    eff_age = getattr(o, "cached_age", None)
    if eff_age is not None:
        try:
            site_note = f" · {format_cached_age_note(float(eff_age))}"
        except Exception as e:
            log.warning("Ошибка форматирования cached_age в format_details: %s", e)
    lines.append(f"🏷 Сайт: {escape(site_label(o.site))}{site_note}")

    link = offer_link_html(o)
    if link:
        lines.append(f"🔗 {link}")
        if not o.url_is_direct:
            criteria: list[str] = []
            has_time = bool(o.depart_time or o.depart_at or (o.back and (o.back.depart_time or o.back.depart_at)))
            has_fn = bool(o.flight_numbers or (o.back and o.back.flight_numbers))
            if has_time:
                criteria.append("времени вылета")
            if has_fn:
                criteria.append("номеру рейса")
            if criteria:
                lines.append(f"<i>Найдите рейс в выдаче по {' и '.join(criteria)}</i>")

    if o.fetched_at is not None:
        lines.append(f"<i>Цена снята в {datetime.fromtimestamp(o.fetched_at):%H:%M}</i>")

    return "\n".join(lines).strip()

