from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Sequence

from .criteria import SearchCriteria
from .filters import pareto_front, pick_cards
from .models import Leg, Offer, Query, SiteResult
from .offers import baggage_state, compute_offer_id
from .parsing import city_name

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


def _error_line(r: SiteResult) -> str:
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
}

MONTHS_SHORT = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
CARD_ICONS = {"Самый дешёвый": "💰", "Самый быстрый": "⚡", "Лучший баланс": "⚖️"}
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


def offer_card(
    o: Offer,
    q: Query | None,
    *,
    number: int,
    title: str | None = None,
    baggage_mode: str = "any",
) -> list[str]:
    """Карточка билета: заголовок, цена и авиакомпания, плечи в цитате, ссылка."""
    icon = CARD_ICONS.get(title or "", "🎫")
    head = f"<b>{number}  {icon} {escape(title)}</b>" if title else f"<b>{number}</b>"
    price_row = _price_html(o)
    airline = _airline_label(o)
    if airline:
        price_row += f"  ·  {airline}"
    lines = [head, price_row]
    body = offer_body(o, q, baggage_mode)
    if body:
        lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    link = offer_link_html(o)
    src = f"<i>{escape(site_label(o.site))}</i>"
    lines.append(f"🔗 {link}  ·  {src}" if link else src)
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


def _site_issue(r: SiteResult) -> str:
    label = f"<b>{escape(site_label(r.site))}</b>"
    link = f' · <a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>' if r.page_url else ""
    if not r.ok:
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
    top_n: int = 5,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
) -> tuple[list[str], list[Offer]]:
    """Полный список по сайтам (каждая часть ≤ 4000 символов) и плоский список показанных билетов."""
    header_lines = _trip_head(q)
    if baggage_mode != "any":
        header_lines.append(_baggage_filter_line(baggage_mode, dropped))
    all_offers = [o for r in results for o in r.offers]
    if all_offers:
        best = min(all_offers, key=lambda o: o.price)
        header_lines.append(
            f"🏆 Дешевле всего: <b>{money(best.price, best.currency)}</b> · {escape(site_label(best.site))}"
        )
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
    for r in results:
        if not r.ok or not r.offers:
            issues.append(_site_issue(r))
            continue

        notes: list[str] = []
        if r.partial:
            notes.append("неполная выдача: сайт ещё догружал варианты")
        if r.cached:
            notes.append("из кэша, цены могли измениться")
        site_head = [f"<b>{escape(site_label(r.site))}</b>  <i>{r.elapsed:.0f} с</i>"]
        site_head += [f"<i>{escape(n)}</i>" for n in notes]

        seen: set[tuple[str, ...]] = set()
        blocks: list[tuple[Offer, list[str]]] = []
        for o in r.offers:
            body = offer_body(o, q, baggage_mode)
            key = (str(o.price), _airline_label(o) or "", *body)
            if key in seen:
                continue
            seen.add(key)
            row = f"<b>{{n}}.</b> {_price_html(o)}"
            airline = _airline_label(o)
            if airline:
                row += f"  ·  {airline}"
            blocks.append((o, [row] + [f"    {b}" for b in body]))
            if len(blocks) >= top_n:
                break

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

    footer = _ozon_footer(q, results) + [
        "<i>Цены сняты сейчас, при покупке могут измениться.</i>",
    ]
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
    top_n: int = 5,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
) -> str:
    """Формирует текстовый отчёт со сквозной нумерацией вариантов."""
    parts, _ = build_report(q, results, top_n, baggage_mode=baggage_mode, dropped=dropped)
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
) -> tuple[list[str], list[Offer], list[Offer]]:
    """Сводка с карточками (pick_cards), плоским списком (flat) и пулом билетов (pool)."""
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

    pareto = pareto_front(unique_offers)
    pareto_set = {id(o) for o in pareto}
    others = [o for o in unique_offers if id(o) not in pareto_set]
    others.sort(key=lambda o: (o.currency, o.price))
    pool = pareto + others

    cards = pick_cards(pool, criteria)
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

    card_blocks: list[list[str]] = []
    for counter, (title, o) in enumerate(cards, start=1):
        card = offer_card(o, q, number=counter, title=title, baggage_mode=baggage_mode)
        card.append("")
        card_blocks.append(card)

    issue_lines = [_site_issue(r) for r in results if not r.ok]

    empty_lines: list[str] = []
    if not pool:
        empty_lines.append("😕 <b>Билетов не нашлось</b>")
        if not criteria.is_default():
            empty_lines.append("Попробуйте ослабить фильтры.")
        empty_lines.append("")

    footer_lines = _ozon_footer(q, results) + [
        "<i>Цены сняты сейчас, при покупке могут измениться.</i>",
    ]
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
    lines.append(f"🏷 Сайт: {escape(site_label(o.site))}")

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


def format_late_site_notification(
    site: str,
    price: int,
    currency: str,
    card_min_price: int | None,
    card_min_currency: str = "RUB",
    page_url: str | None = None,
) -> str:
    """Форматирует отдельное короткое сообщение об опоздавшем сайте, нашедшем билет дешевле."""
    price_str = money(price, currency)
    card_str = money(card_min_price, card_min_currency) if card_min_price is not None else "—"
    link = f' — <a href="{escape(page_url, quote=True)}">открыть выдачу</a>' if page_url else ""
    return f"➕ Опоздал сайт {escape(site)}: нашёл дешевле — {price_str} (в карточке было {card_str}){link}"

