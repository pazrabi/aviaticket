"""Парсер живой текстовой выдачи City Travel (city.travel).

Страница выдачи отдаётся сервером готовым HTML, цены в рублях (₽),
перехватываемых JSON-ответов нет, поэтому разбор выполняется по тексту
страницы (snapshot.text).
"""
from __future__ import annotations

from datetime import date
import logging
import re
from typing import Any

from ..collector import Snapshot
from ..filters import parse_duration_minutes
from ..models import Leg, Offer, Query
from ..offers import compute_offer_id
from . import ParsedOffers, register_parser
from .travelpayouts import format_duration

logger = logging.getLogger(__name__)

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_RE = re.compile(r"^(\d{1,2})\s+([а-яА-ЯёЁ]+)\.?$")
IATA_RE = re.compile(r"^[A-Za-z]{3}$")
PASSENGER_RE = re.compile(r"пассажир", re.IGNORECASE)
FOOTER_MARKER = "ПОЛЬЗОВАТЕЛЯМ"

MONTHS_RU = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}

HEADER_IGNORE_PREFIXES = (
    "лучшая цена",
    "сначала дешёв",
    "сначала дешев",
    "сначала дорог",
    "сначала быстр",
    "фильтр",
    "сортировк",
    "назад",
    "билеты",
    "авиабилеты",
    "найти",
    "поиск",
    "город",
    "вылет",
    "прилёт",
    "прилет",
    "пассажир",
    "туда и обратно",
)


def _is_time(line: str) -> bool:
    return bool(TIME_RE.match(line.strip()))


def _is_date(line: str) -> bool:
    return bool(DATE_RE.match(line.strip()))


def _is_iata(line: str) -> bool:
    return bool(IATA_RE.match(line.strip()))


def _is_duration(line: str) -> bool:
    cleaned = line.strip()
    return bool(re.search(r"\b\d+\s*(?:ч|мин|м|д|d|h|min)\b", cleaned, re.IGNORECASE))


def _is_transfers(line: str) -> bool:
    cleaned = line.strip().lower()
    return "прям" in cleaned or "пересад" in cleaned


def _is_header_or_badge(line: str) -> bool:
    cleaned = line.strip().lower()
    return any(cleaned.startswith(prefix) for prefix in HEADER_IGNORE_PREFIXES)


def _is_leg_start(lines: list[str], idx: int) -> bool:
    if idx + 2 >= len(lines):
        return False
    return _is_time(lines[idx]) and _is_date(lines[idx + 1]) and _is_iata(lines[idx + 2])


def _parse_date(text: str, base_date: date | None) -> date | None:
    m = DATE_RE.match(text.strip())
    if not m:
        return None
    day = int(m.group(1))
    mon_key = m.group(2).lower()[:3]
    month = MONTHS_RU.get(mon_key)
    if not month:
        return None
    if base_date is None:
        return None
    year = base_date.year
    if month < base_date.month and (base_date.month - month >= 6):
        year += 1
    elif month > base_date.month and (month - base_date.month >= 6):
        year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_duration(text: str | None) -> str | None:
    if not text:
        return None
    mins = parse_duration_minutes(text)
    if mins is not None:
        return format_duration(mins)
    cleaned = text.replace("\xa0", " ").strip()
    hours_m = re.search(r"(\d+)\s*ч", cleaned)
    mins_m = re.search(r"(\d+)\s*м", cleaned)
    if hours_m or mins_m:
        h = int(hours_m.group(1)) if hours_m else 0
        m = int(mins_m.group(1)) if mins_m else 0
        return format_duration(h * 60 + m)
    return None


def _parse_transfers(text: str) -> int:
    cleaned = text.strip().lower()
    if "прям" in cleaned or "без пересад" in cleaned:
        return 0
    m = re.search(r"(\d+)", cleaned)
    if m:
        return int(m.group(1))
    if "пересад" in cleaned:
        return 1
    return 0


def _extract_layovers(raw_items: list[str]) -> list[str] | None:
    layovers: list[str] = []
    pattern = re.compile(r"[^,]+,\s*\d+[\w\s]*", re.UNICODE)
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        matches = [m.strip() for m in pattern.findall(item) if m.strip()]
        if matches:
            layovers.extend(matches)
        else:
            parts = [p.strip() for p in item.split(",") if p.strip()]
            if len(parts) > 1 and not any(re.search(r"\d", p) for p in parts):
                layovers.extend(parts)
            else:
                layovers.append(item)
    return layovers if layovers else None


def _build_search_url(query: Query | None, snapshot: Snapshot | None) -> str | None:
    """Формирует прямую ссылку на выдачу поиска City Travel."""
    if snapshot and snapshot.url and "city.travel" in snapshot.url:
        return snapshot.url
    if query and query.origin and query.destination and query.depart:
        dd_mm_yyyy = query.depart.strftime("%d.%m.%Y")
        adults = query.adults
        if query.return_date:
            dd_mm_yyyy_ret = query.return_date.strftime("%d.%m.%Y")
            return (
                f"https://city.travel/avia/search/{query.origin}/{query.destination}/E/"
                f"{dd_mm_yyyy}/{dd_mm_yyyy_ret}/{adults}-0-0/0/0/0/00000000000000000000000000000000"
                f"?rt=RTA{query.destination}"
            )
        return (
            f"https://city.travel/avia/search/{query.origin}/{query.destination}/E/"
            f"{dd_mm_yyyy}/0/{adults}-0-0/0/0/0/00000000000000000000000000000000"
        )
    if snapshot and snapshot.url:
        return snapshot.url
    return None


def _parse_leg_block(
    lines: list[str],
    idx: int,
    base_date: date | None,
) -> tuple[dict[str, Any] | None, int]:
    """Разбирает блок плеча из 8 строк плюс возможные пересадки."""
    if idx + 7 >= len(lines):
        return None, idx
    if not _is_leg_start(lines, idx):
        return None, idx

    dep_time = lines[idx].strip()
    dep_date_str = lines[idx + 1].strip()
    dep_iata = lines[idx + 2].strip().upper()

    line3 = lines[idx + 3].strip()
    line4 = lines[idx + 4].strip()
    if _is_duration(line3):
        duration_str = line3
        transfers_str = line4
    elif _is_duration(line4):
        duration_str = line4
        transfers_str = line3
    else:
        duration_str = line3
        transfers_str = line4

    arr_iata = lines[idx + 5].strip().upper()
    arr_time = lines[idx + 6].strip()
    arr_date_str = lines[idx + 7].strip()

    if not _is_time(arr_time):
        return None, idx

    dep_date = _parse_date(dep_date_str, base_date)
    arr_date = _parse_date(arr_date_str, dep_date or base_date)

    dep_at = f"{dep_date.isoformat()}T{dep_time}" if dep_date and dep_time else None
    arr_at = f"{arr_date.isoformat()}T{arr_time}" if arr_date and arr_time else None

    duration = _parse_duration(duration_str)
    transfers = _parse_transfers(transfers_str)

    cur_idx = idx + 8
    layover_raw: list[str] = []
    while cur_idx < len(lines):
        line = lines[cur_idx]
        if _is_leg_start(lines, cur_idx) or PASSENGER_RE.search(line) or "₽" in line:
            break
        layover_raw.append(line)
        cur_idx += 1

    layovers = _extract_layovers(layover_raw) if transfers > 0 else None

    return {
        "depart_time": dep_time,
        "arrive_time": arr_time,
        "duration": duration,
        "transfers": transfers,
        "layovers": layovers,
        "depart_at": dep_at,
        "arrive_at": arr_at,
        "origin": dep_iata,
        "destination": arr_iata,
    }, cur_idx


def results_complete(snapshot: Snapshot | None) -> bool:
    """Выдача City Travel считается завершённой, если в тексте страницы есть результаты (цены в ₽ или футер)."""
    if not snapshot or not snapshot.text or getattr(snapshot, "blocked", False):
        return False
    text = snapshot.text
    return "₽" in text or FOOTER_MARKER in text


@register_parser("city_travel")
def parse_city_travel(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает текстовое представление страницы City Travel в список моделей Offer."""
    if not snapshot or getattr(snapshot, "blocked", False):
        return ParsedOffers(total=0, skipped=0)

    text = getattr(snapshot, "text", "") or ""
    if not text:
        return ParsedOffers(total=0, skipped=0)

    raw_lines = [line.replace("\xa0", " ").replace("\u2009", " ").replace("\u202f", " ").strip() for line in text.splitlines()]
    clean_lines: list[str] = []
    for line in raw_lines:
        if not line:
            continue
        if line.upper().startswith(FOOTER_MARKER):
            break
        clean_lines.append(line)

    n = len(clean_lines)
    if n == 0:
        return ParsedOffers(total=0, skipped=0)

    url = _build_search_url(query, snapshot)
    is_roundtrip = query is not None and query.return_date is not None
    offers: list[Offer] = []
    total = 0

    i = 0
    prev_end = 0

    while i < n:
        if not _is_leg_start(clean_lines, i):
            i += 1
            continue

        airline = None
        for k in range(i - 1, prev_end - 1, -1):
            cand = clean_lines[k]
            if _is_header_or_badge(cand):
                continue
            airline = cand
            break

        leg1, cur = _parse_leg_block(clean_lines, i, query.depart if query else None)
        if not leg1:
            i += 1
            continue

        total += 1

        leg2 = None
        if cur < n and _is_leg_start(clean_lines, cur):
            base_back = query.return_date if query else (query.depart if query else None)
            leg2, cur = _parse_leg_block(clean_lines, cur, base_back)

        if query is not None:
            if is_roundtrip and leg2 is None:
                i = cur
                prev_end = cur
                continue
            if not is_roundtrip and leg2 is not None:
                i = cur
                prev_end = cur
                continue

        while cur < n and (PASSENGER_RE.search(clean_lines[cur]) or not ("₽" in clean_lines[cur])):
            if _is_leg_start(clean_lines, cur):
                break
            cur += 1

        if cur >= n or "₽" not in clean_lines[cur]:
            i = cur
            prev_end = cur
            continue

        digits = "".join(re.findall(r"\d+", clean_lines[cur]))
        if not digits:
            i = cur + 1
            prev_end = cur + 1
            continue
        try:
            card_price = int(digits)
        except ValueError:
            i = cur + 1
            prev_end = cur + 1
            continue

        if card_price <= 0:
            i = cur + 1
            prev_end = cur + 1
            continue

        cur += 1

        back_leg: Leg | None = None
        if leg2 is not None:
            back_airline = airline
            if airline and "," in airline:
                parts = [p.strip() for p in airline.split(",") if p.strip()]
                if len(parts) >= 2:
                    back_airline = parts[1]
            back_leg = Leg(
                airline=back_airline,
                depart_time=leg2["depart_time"],
                arrive_time=leg2["arrive_time"],
                duration=leg2["duration"],
                transfers=leg2["transfers"],
                flight_numbers=None,
                aircraft=None,
                layovers=leg2["layovers"],
                depart_at=leg2["depart_at"],
                arrive_at=leg2["arrive_at"],
            )

        offer = Offer(
            site="city_travel",
            price=card_price,
            currency="RUB",
            airline=airline,
            depart_time=leg1["depart_time"],
            arrive_time=leg1["arrive_time"],
            duration=leg1["duration"],
            transfers=leg1["transfers"],
            baggage=None,
            carry_on=None,
            url=url,
            flight_numbers=None,
            aircraft=None,
            layovers=leg1["layovers"],
            depart_at=leg1["depart_at"],
            arrive_at=leg1["arrive_at"],
            back=back_leg,
        )
        offer.offer_id = compute_offer_id(offer)
        offers.append(offer)

        i = cur
        prev_end = cur

    offers_by_id: dict[str, Offer] = {}
    for o in offers:
        if o.offer_id in offers_by_id:
            if o.price < offers_by_id[o.offer_id].price:
                offers_by_id[o.offer_id] = o
        else:
            offers_by_id[o.offer_id] = o

    result_offers = list(offers_by_id.values())
    result_offers.sort(key=lambda o: o.price)
    return ParsedOffers(result_offers, total=total, skipped=total - len(result_offers))


parse_city_travel.results_complete = results_complete  # type: ignore[attr-defined]
