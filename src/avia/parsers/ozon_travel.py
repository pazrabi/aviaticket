"""Парсер выдачи Озон Тревел (ozon.ru/travel) по ответам виджета aviaSearchResult."""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import re
from typing import Any
import urllib.parse

from ..collector import Snapshot
from ..models import Leg, Offer, Query
from ..offers import compute_offer_id
from . import register_parser
from .travelpayouts import format_duration

logger = logging.getLogger(__name__)

SUPERSCRIPT_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
WIDGET_MARKER = "widgetStateId=aviaSearchResult"
OZON_BASE_URL = "https://www.ozon.ru"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}$")
PRICE_RE = re.compile(r"^\d+₽$")
LAYOVER_RE = re.compile(r",\s*\d+(\.\d+)?\s*(?:ч|м|д)")


def _parse_duration(text: str | None) -> str | None:
    """Разбирает текстовую длительность вида 'в пути 20ч 25м', 'в пути 1д 1ч 55м' в формат '20ч 25м'."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    days_m = re.search(r"(\d+)\s*д", cleaned)
    hours_m = re.search(r"(\d+)\s*ч", cleaned)
    mins_m = re.search(r"(\d+)\s*м", cleaned)
    if not hours_m and not mins_m and not days_m:
        return None
    days = int(days_m.group(1)) if days_m else 0
    hours = int(hours_m.group(1)) if hours_m else 0
    mins = int(mins_m.group(1)) if mins_m else 0
    total_minutes = days * 24 * 60 + hours * 60 + mins
    return format_duration(total_minutes)


def _parse_transfers_and_layovers(transfers_list: list[Any] | None) -> tuple[int, list[str] | None]:
    """Извлекает количество пересадок и список городов пересадок с длительностью."""
    if not transfers_list or not isinstance(transfers_list, list):
        return 0, None

    first_item = transfers_list[0]
    first_text = ""
    if isinstance(first_item, dict):
        city_dict = first_item.get("city")
        if isinstance(city_dict, dict):
            first_text = city_dict.get("text", "")

    first_lower = first_text.lower().strip()
    if "прям" in first_lower or "без" in first_lower:
        transfers_count = 0
    else:
        m = re.search(r"(\d+)", first_lower)
        if m:
            transfers_count = int(m.group(1))
        else:
            transfers_count = max(0, len(transfers_list) - 1)

    layovers: list[str] = []
    for item in transfers_list[1:]:
        if isinstance(item, dict):
            city_dict = item.get("city")
            if isinstance(city_dict, dict):
                city_str = city_dict.get("text", "").strip()
                if city_str:
                    layovers.append(city_str)

    if transfers_count == 0:
        return 0, None
    return transfers_count, (layovers if layovers else None)


def _parse_baggage(luggage_obj: Any) -> str | None:
    """Определяет, включён ли багаж в тариф.

    В Ozon Travel доплата за багаж отображается как 'Багаж 10 кг +6 181 ₽',
    что означает, что багаж НЕ включён в стоимость билета ('без багажа').
    """
    if isinstance(luggage_obj, str):
        text = luggage_obj
    elif isinstance(luggage_obj, dict):
        text = luggage_obj.get("text")
    else:
        return None
    if not text or not isinstance(text, str):
        return None

    text_clean = text.replace("\xa0", " ").replace("\u2009", " ").strip()
    text_lower = text_clean.lower()

    if re.search(r"\+\s*\d+", text_clean) or "доплат" in text_lower:
        return "без багажа"
    if "не включ" in text_lower or "платн" in text_lower:
        return "без багажа"
    if "ручная кладь" in text_lower and "багаж" not in text_lower:
        return "без багажа"
    if "без багажа" in text_lower:
        return "без багажа"
    if "с багажом" in text_lower or "багаж" in text_lower or "включен" in text_lower or "включён" in text_lower:
        return "с багажом"

    return None


def _parse_leg(details: Any, base_date: Any) -> dict[str, Any]:
    """Разбирает блок одного направления (wayThereDetails / wayBackDetails)."""
    if not isinstance(details, dict):
        details = {}

    time_obj = details.get("tripTime")
    time_text = time_obj.get("text", "") if isinstance(time_obj, dict) else ""
    dep_time = arr_time = None
    if time_text:
        parts = re.split(r"\s*[-–—]\s*", time_text.strip())
        if len(parts) >= 2:
            dep_time, arr_time = parts[0].strip(), parts[1].strip()
        elif ":" in parts[0]:
            dep_time = parts[0].strip()

    days_offset = 0
    tdiff_obj = details.get("timeDifference")
    if isinstance(tdiff_obj, dict):
        m = re.search(r"[+-]?\d+", (tdiff_obj.get("text") or "").translate(SUPERSCRIPT_TRANS))
        if m:
            days_offset = int(m.group(0))

    dep_at = arr_at = None
    if base_date is not None:
        if dep_time:
            dep_at = f"{base_date.isoformat()}T{dep_time}"
        if arr_time:
            arr_at = f"{(base_date + timedelta(days=days_offset)).isoformat()}T{arr_time}"

    dur_obj = details.get("tripDuration")
    duration = _parse_duration(dur_obj.get("text") if isinstance(dur_obj, dict) else None)
    transfers, layovers = _parse_transfers_and_layovers(details.get("transfers"))
    return {
        "depart_time": dep_time,
        "arrive_time": arr_time,
        "depart_at": dep_at,
        "arrive_at": arr_at,
        "duration": duration,
        "transfers": transfers,
        "layovers": layovers,
    }


def _split_flight_numbers(numbers: list[str] | None, first_count: int) -> tuple[list[str] | None, list[str] | None]:
    """Делит общий список рейсов на туда и обратно по числу сегментов туда (пересадки + 1)."""
    if not numbers:
        return None, None
    there, back = numbers[:first_count], numbers[first_count:]
    return there or None, back or None


def _parse_carry_on(luggage_obj: Any) -> str | None:
    """Ручная кладь: явно указана только в тарифах без багажа ('Ручная кладь 7 кг')."""
    if isinstance(luggage_obj, str):
        text = luggage_obj.replace("\xa0", " ").replace("\u2009", " ").lower()
    elif isinstance(luggage_obj, dict):
        text = str(luggage_obj.get("text") or "").replace("\xa0", " ").replace("\u2009", " ").lower()
    else:
        return None
    if "ручная кладь" in text:
        return "ручная кладь есть"
    return None


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Озон Тревел по снимку сетевых ответов.

    Выдача считается завершённой (True), если среди ответов aviaSearchResult
    есть хотя бы один с непустым списком карточек flights.
    """
    if not snapshot or not snapshot.json_payloads or getattr(snapshot, "blocked", False):
        return False

    for url, body in snapshot.json_payloads:
        if WIDGET_MARKER not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        state = data.get("state")
        if not isinstance(state, dict):
            continue
        flights = state.get("flights")
        if isinstance(flights, list) and len(flights) > 0:
            return True

    return False


def _parse_text_leg(
    lines: list[str],
    start_idx: int,
    base_date: Any,
) -> tuple[dict[str, Any] | None, int]:
    """Разбирает блок одного направления из строк текста страницы."""
    n = len(lines)
    if start_idx >= n:
        return None, start_idx

    m_time = re.search(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", lines[start_idx])
    if not m_time:
        return None, start_idx
    dep_time, arr_time = m_time.group(1), m_time.group(2)
    j = start_idx + 1

    days_offset = 0
    if j < n:
        trans = lines[j].translate(SUPERSCRIPT_TRANS).strip()
        m_off = re.fullmatch(r"[+-]?\d+", trans)
        if m_off:
            days_offset = int(m_off.group(0))
            j += 1

    # Строка маршрута (KUF – EVN и т.п.)
    if j < n and "в пути" not in lines[j].lower() and ("–" in lines[j] or "-" in lines[j] or "—" in lines[j]):
        j += 1

    dur_str = None
    if j < n and "в пути" in lines[j].lower():
        dur_str = _parse_duration(lines[j])
        j += 1
    elif j < n:
        dur = _parse_duration(lines[j])
        if dur:
            dur_str = dur
            j += 1

    transfers_str = ""
    if j < n and ("пересад" in lines[j].lower() or "прям" in lines[j].lower() or "без" in lines[j].lower()):
        transfers_str = lines[j]
        j += 1

    layovers_list: list[str] = []
    while j < n and LAYOVER_RE.search(lines[j]):
        layovers_list.append(lines[j].strip())
        j += 1

    t_list = [{"city": {"text": transfers_str}}] + [{"city": {"text": lay}} for lay in layovers_list]
    transfers, layovers_parsed = _parse_transfers_and_layovers(t_list)

    dep_at = arr_at = None
    if base_date is not None:
        if dep_time:
            dep_at = f"{base_date.isoformat()}T{dep_time}"
        if arr_time:
            arr_at = f"{(base_date + timedelta(days=days_offset)).isoformat()}T{arr_time}"

    return {
        "depart_time": dep_time,
        "arrive_time": arr_time,
        "depart_at": dep_at,
        "arrive_at": arr_at,
        "duration": dur_str,
        "transfers": transfers,
        "layovers": layovers_parsed,
    }, j


def _parse_ozon_travel_text(text: str | None, query: Query | None) -> list[Offer]:
    """Разбирает карточки рейсов из текстового представления страницы Ozon Travel."""
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    n = len(lines)
    if n == 0:
        return []

    round_trip = query is not None and query.return_date is not None
    offers: list[Offer] = []
    i = 0

    while i < n:
        clean_price = re.sub(r"[\s\u2009\u00a0]+", "", lines[i])
        if not PRICE_RE.fullmatch(clean_price):
            i += 1
            continue

        # Найдена строка цены. Ищем строку времени рейса в пределах следующих строк
        time_idx = None
        for k in range(i + 1, min(i + 12, n)):
            if TIME_RE.match(lines[k]):
                time_idx = k
                break

        if time_idx is None or time_idx - 1 <= i:
            i += 1
            continue

        airline_line = lines[time_idx - 1].strip()
        clean_airline = re.sub(r"[\s\u2009\u00a0]+", "", airline_line)
        if PRICE_RE.fullmatch(clean_airline):
            i += 1
            continue

        digits = "".join(re.findall(r"\d+", lines[i]))
        if not digits:
            i += 1
            continue
        card_price = int(digits)
        if card_price <= 0:
            i += 1
            continue

        baggage_line: str | None = None
        for mid_idx in range(i + 1, time_idx - 1):
            line_mid = lines[mid_idx]
            clean_mid = re.sub(r"[\s\u2009\u00a0]+", "", line_mid)
            if PRICE_RE.fullmatch(clean_mid):
                continue
            if line_mid.startswith("+") and any(c.isdigit() for c in line_mid):
                continue
            if "багаж" in line_mid.lower() or "ручная кладь" in line_mid.lower():
                baggage_line = line_mid

        airline = airline_line or None
        baggage = _parse_baggage(baggage_line) if baggage_line else None
        carry_on = _parse_carry_on(baggage_line) if baggage_line else None

        base_date = query.depart if query is not None else None
        leg1, j = _parse_text_leg(lines, time_idx, base_date)
        if not leg1:
            i += 1
            continue

        back_leg: Leg | None = None
        if j < n and TIME_RE.match(lines[j]):
            back_date = query.return_date if query is not None else None
            leg2, j = _parse_text_leg(lines, j, back_date)
            if leg2:
                back_leg = Leg(
                    depart_time=leg2["depart_time"],
                    arrive_time=leg2["arrive_time"],
                    duration=leg2["duration"],
                    transfers=leg2["transfers"],
                    flight_numbers=None,
                    layovers=leg2["layovers"],
                    depart_at=leg2["depart_at"],
                    arrive_at=leg2["arrive_at"],
                )

        if round_trip:
            if back_leg is None:
                i = j
                continue
        else:
            if back_leg is not None:
                i = j
                continue

        offer = Offer(
            site="ozon_travel",
            price=card_price,
            currency="RUB",
            airline=airline,
            depart_time=leg1["depart_time"],
            arrive_time=leg1["arrive_time"],
            duration=leg1["duration"],
            transfers=leg1["transfers"],
            baggage=baggage,
            carry_on=carry_on,
            url=None,
            flight_numbers=None,
            layovers=leg1["layovers"],
            depart_at=leg1["depart_at"],
            arrive_at=leg1["arrive_at"],
            back=back_leg,
        )
        offer.offer_id = compute_offer_id(offer)
        offers.append(offer)
        i = j

    offers_by_id: dict[str, Offer] = {}
    for o in offers:
        if o.offer_id in offers_by_id:
            if o.price < offers_by_id[o.offer_id].price:
                offers_by_id[o.offer_id] = o
        else:
            offers_by_id[o.offer_id] = o

    result_offers = list(offers_by_id.values())
    result_offers.sort(key=lambda o: o.price)
    return result_offers


@register_parser("ozon_travel")
def parse_ozon_travel(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает сетевые ответы виджета aviaSearchResult Озон Тревел в список Offer."""
    if not snapshot or getattr(snapshot, "blocked", False):
        return []

    round_trip = query is not None and query.return_date is not None

    # Одни и те же виджет-ответы приходят несколько раз с обновлением;
    # выбираем ответ с наибольшим числом карточек (при равенстве более поздний).
    # Ответ с asyncErrorMessage без flights игнорируем.
    has_async_error = False
    best_flights: list[dict[str, Any]] | None = None
    best_count = -1

    if snapshot.json_payloads:
        for url, body in snapshot.json_payloads:
            if WIDGET_MARKER not in url:
                continue
            try:
                data = json.loads(body)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            state = data.get("state")
            if not isinstance(state, dict):
                continue

            flights_raw = state.get("flights")
            async_error = state.get("asyncErrorMessage")

            if async_error is not None and not flights_raw:
                has_async_error = True
                continue

            if not isinstance(flights_raw, list):
                continue

            count = len(flights_raw)
            if count >= best_count:
                best_count = count
                best_flights = flights_raw

    if has_async_error and not best_flights:
        return []

    if best_flights:
        offers: list[Offer] = []
        for card in best_flights:
            if not isinstance(card, dict):
                continue
            try:
                price_container = card.get("price")
                if not isinstance(price_container, dict):
                    continue
                price_list = price_container.get("price")
                if not isinstance(price_list, list) or not price_list:
                    continue

                # Ищем цену со textStyle == "PRICE"
                p_text = None
                for p_item in price_list:
                    if isinstance(p_item, dict) and p_item.get("textStyle") == "PRICE":
                        p_text = p_item.get("text")
                        break
                if not p_text:
                    for p_item in price_list:
                        if isinstance(p_item, dict) and p_item.get("text"):
                            p_text = p_item.get("text")
                            break

                if not p_text:
                    continue

                digits = "".join(re.findall(r"\d+", p_text))
                if not digits:
                    continue
                price = int(digits)
                if price <= 0:
                    continue

                airlines_container = card.get("airlinesText")
                airline = None
                if isinstance(airlines_container, dict):
                    a_text = airlines_container.get("text")
                    if a_text and isinstance(a_text, str):
                        airline = a_text.strip() or None

                there = _parse_leg(card.get("wayThereDetails"), query.depart if query is not None else None)
                back_info = None
                if round_trip:
                    back_details = card.get("wayBackDetails")
                    if not isinstance(back_details, dict):
                        continue
                    back_info = _parse_leg(back_details, query.return_date)

                luggage_obj = card.get("luggageText")
                baggage = _parse_baggage(luggage_obj)

                click_obj = card.get("cardClickAction")
                link = click_obj.get("link") if isinstance(click_obj, dict) else None
                offer_url = None
                flight_numbers = None
                if link and isinstance(link, str):
                    link = link.strip()
                    if link.startswith("http://") or link.startswith("https://"):
                        offer_url = link
                    elif link.startswith("/"):
                        offer_url = f"{OZON_BASE_URL}{link}"
                    else:
                        offer_url = f"{OZON_BASE_URL}/{link}"

                    try:
                        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
                        fn_raw = qs.get("flightNumbers", [None])[0]
                        if fn_raw:
                            flight_numbers = [fn.strip() for fn in fn_raw.split(";") if fn.strip()] or None
                    except Exception:
                        flight_numbers = None

                back_leg = None
                if back_info is not None:
                    flight_numbers, back_numbers = _split_flight_numbers(flight_numbers, there["transfers"] + 1)
                    back_leg = Leg(
                        depart_time=back_info["depart_time"],
                        arrive_time=back_info["arrive_time"],
                        duration=back_info["duration"],
                        transfers=back_info["transfers"],
                        flight_numbers=back_numbers,
                        layovers=back_info["layovers"],
                        depart_at=back_info["depart_at"],
                        arrive_at=back_info["arrive_at"],
                    )

                offer = Offer(
                    site="ozon_travel",
                    price=price,
                    currency="RUB",
                    airline=airline,
                    depart_time=there["depart_time"],
                    arrive_time=there["arrive_time"],
                    duration=there["duration"],
                    transfers=there["transfers"],
                    baggage=baggage,
                    carry_on=_parse_carry_on(luggage_obj),
                    url=offer_url,
                    flight_numbers=flight_numbers,
                    layovers=there["layovers"],
                    depart_at=there["depart_at"],
                    arrive_at=there["arrive_at"],
                    back=back_leg,
                )
                offer.offer_id = compute_offer_id(offer)
                offers.append(offer)
            except Exception:
                continue

        if offers:
            # Дедупликация: при совпадении offer_id сохраняем предложение с минимальной ценой
            offers_by_id: dict[str, Offer] = {}
            for o in offers:
                if o.offer_id in offers_by_id:
                    if o.price < offers_by_id[o.offer_id].price:
                        offers_by_id[o.offer_id] = o
                else:
                    offers_by_id[o.offer_id] = o

            result_offers = list(offers_by_id.values())
            result_offers.sort(key=lambda o: o.price)
            return result_offers

    # Запасной разбор по тексту страницы (snapshot.text),
    # срабатывает ТОЛЬКО когда по JSON не получилось ни одного оффера
    text = getattr(snapshot, "text", "") or ""
    if text:
        text_offers = _parse_ozon_travel_text(text, query)
        logger.info("ozon: разобрано %d карточек из текста страницы (нет JSON)", len(text_offers))
        return text_offers

    return []


parse_ozon_travel.results_complete = results_complete  # type: ignore[attr-defined]
