"""Парсер живой выдачи Aviasales по XHR-ответам API search/v3.2/results."""
from __future__ import annotations

import json
import re
from typing import Any

from ..collector import Snapshot
from ..models import Leg, Offer, Query
from . import ParsedOffers, register_parser
from .travelpayouts import format_duration


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Безопасно преобразует значение в float (исключая bool)."""
    if val is None or isinstance(val, bool):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _extract_time(dt_str: str | None) -> str | None:
    """Извлекает время вида HH:MM из строки даты/времени (например, 'YYYY-MM-DD HH:MM')."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    if " " in dt_str:
        parts = dt_str.split(" ", 1)
        if len(parts) > 1 and len(parts[1]) >= 5:
            return parts[1][:5]
    if "T" in dt_str:
        parts = dt_str.split("T", 1)
        if len(parts) > 1 and len(parts[1]) >= 5:
            return parts[1][:5]
    return dt_str[:5] if len(dt_str) >= 5 else None


def _get_airline_name(carrier_code: str | None, airlines: dict[str, Any]) -> str | None:
    """Возвращает русское название авиакомпании по коду перевозчика."""
    if not carrier_code or not isinstance(carrier_code, str):
        return None
    airline_info = airlines.get(carrier_code)
    if isinstance(airline_info, dict):
        name_info = airline_info.get("name")
        if isinstance(name_info, dict):
            ru_info = name_info.get("ru")
            if isinstance(ru_info, dict):
                default_name = ru_info.get("default")
                if default_name:
                    return str(default_name)
            # Запасной вариант: берем любое имеющееся значение
            for val in name_info.values():
                if isinstance(val, dict) and "default" in val:
                    return str(val["default"])
                if isinstance(val, str):
                    return val
        elif isinstance(name_info, str):
            return name_info
    return carrier_code


def _format_iso_datetime(dt_str: str | None) -> str | None:
    """Преобразует строку даты/времени в формат ISO (например, '2026-12-16T10:15')."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    cleaned = dt_str.strip()
    if not cleaned:
        return None
    if " " in cleaned:
        cleaned = cleaned.replace(" ", "T", 1)
    return cleaned


def _get_place_name(code: str | None, places: dict[str, Any]) -> str:
    """Возвращает русское название города по коду аэропорта или города из chunk.places."""
    if not code or not isinstance(code, str):
        return ""
    if not isinstance(places, dict):
        return code

    city_code = code
    airports = places.get("airports")
    if isinstance(airports, dict):
        airport = airports.get(code)
        if isinstance(airport, dict):
            c_code = airport.get("city_code")
            if c_code and isinstance(c_code, str):
                city_code = c_code

    cities = places.get("cities")
    if isinstance(cities, dict):
        for key in (city_code, code):
            city = cities.get(key)
            if isinstance(city, dict):
                name_info = city.get("name")
                if isinstance(name_info, dict):
                    ru_info = name_info.get("ru")
                    if isinstance(ru_info, dict):
                        default_name = ru_info.get("default")
                        if default_name:
                            return str(default_name)
                    for val in name_info.values():
                        if isinstance(val, dict) and "default" in val:
                            return str(val["default"])
                        if isinstance(val, str):
                            return val
                elif isinstance(name_info, str):
                    return name_info

    return code


def _format_layover_time(diff_sec: float | int) -> str:
    """Форматирует длительность пересадки вида '2 ч 10 мин', '16 ч' или '45 мин'."""
    if diff_sec <= 0:
        return "0 мин"
    diff_min = int(diff_sec // 60)
    h = diff_min // 60
    m = diff_min % 60
    if h > 0 and m > 0:
        return f"{h} ч {m} мин"
    if h > 0:
        return f"{h} ч"
    return f"{m} мин"


def _parse_segment(
    seg: Any,
    flight_legs: list[dict[str, Any]],
    airlines: dict[str, Any],
    places: dict[str, Any],
) -> dict[str, Any] | None:
    """Разбирает сегмент билета и возвращает словарь с характеристиками плеча."""
    if not isinstance(seg, dict):
        return None
    flight_indices = seg.get("flights")
    if not isinstance(flight_indices, list) or not flight_indices:
        return None

    # Проверяем корректность индексов в flight_legs
    valid_legs: list[tuple[int, dict[str, Any]]] = []
    for idx in flight_indices:
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= len(flight_legs):
            return None
        leg = flight_legs[idx]
        if not isinstance(leg, dict):
            return None
        valid_legs.append((idx, leg))

    if not valid_legs:
        return None

    # Сортируем перелёты по departure_unix_timestamp
    sorted_legs = sorted(
        valid_legs,
        key=lambda item: _safe_float(item[1].get("departure_unix_timestamp")),
    )

    first_f_idx, first_leg = sorted_legs[0]
    last_f_idx, last_leg = sorted_legs[-1]

    # Пересадки
    transfers = len(sorted_legs) - 1

    # Время вылета и прилёта (HH:MM)
    depart_time = _extract_time(first_leg.get("local_departure_date_time"))
    arrive_time = _extract_time(last_leg.get("local_arrival_date_time"))

    # Длительность
    duration = None
    first_dep_ts = _safe_float(first_leg.get("departure_unix_timestamp"), -1.0)
    last_arr_ts = _safe_float(last_leg.get("arrival_unix_timestamp"), -1.0)
    if first_dep_ts > 0 and last_arr_ts > first_dep_ts:
        diff_sec = last_arr_ts - first_dep_ts
        duration = format_duration(int(diff_sec // 60))

    # Авиакомпания
    carrier_code = None
    carr_des = first_leg.get("operating_carrier_designator")
    if isinstance(carr_des, dict):
        carrier_code = carr_des.get("carrier")
    if not carrier_code:
        for _, leg in sorted_legs:
            cd = leg.get("operating_carrier_designator")
            if isinstance(cd, dict) and cd.get("carrier"):
                carrier_code = cd.get("carrier")
                break
    airline = _get_airline_name(carrier_code, airlines)

    # Номера рейсов ("3F 3384" — carrier + number по каждому перелёту сегмента)
    fn_list: list[str] = []
    for _, leg in sorted_legs:
        cd = leg.get("operating_carrier_designator")
        if isinstance(cd, dict):
            carrier = cd.get("carrier")
            number = cd.get("number")
            if carrier and number:
                fn_list.append(f"{carrier} {number}")
            elif carrier:
                fn_list.append(str(carrier))
            elif number:
                fn_list.append(str(number))
    flight_numbers = fn_list if fn_list else None

    # Самолёты (equipment.name уникальные через ", " либо None)
    unique_equipment: list[str] = []
    for _, leg in sorted_legs:
        eq = leg.get("equipment")
        if isinstance(eq, dict):
            name = eq.get("name")
            if name and isinstance(name, str):
                s_name = name.strip()
                if s_name and s_name not in unique_equipment:
                    unique_equipment.append(s_name)
    aircraft = ", ".join(unique_equipment) if unique_equipment else None

    # Описание пересадок (город/аэропорт, время ожидания)
    layover_list: list[str] = []
    for i in range(len(sorted_legs) - 1):
        _, prev_l = sorted_legs[i]
        _, next_l = sorted_legs[i + 1]
        dest_code = prev_l.get("destination")
        place_name = _get_place_name(dest_code, places)

        p_arr = _safe_float(prev_l.get("arrival_unix_timestamp"), -1.0)
        n_dep = _safe_float(next_l.get("departure_unix_timestamp"), -1.0)
        if p_arr > 0 and n_dep >= p_arr:
            time_str = _format_layover_time(n_dep - p_arr)
            layover_list.append(f"{place_name}, {time_str}")
        elif place_name:
            layover_list.append(place_name)
    layovers = layover_list if layover_list else None

    # ISO-даты вылета и прилёта ("2026-12-16T10:15")
    depart_at = _format_iso_datetime(first_leg.get("local_departure_date_time"))
    arrive_at = _format_iso_datetime(last_leg.get("local_arrival_date_time"))

    return {
        "sorted_legs": sorted_legs,
        "first_f_idx": first_f_idx,
        "first_leg": first_leg,
        "last_f_idx": last_f_idx,
        "last_leg": last_leg,
        "transfers": transfers,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
        "duration": duration,
        "airline": airline,
        "flight_numbers": flight_numbers,
        "aircraft": aircraft,
        "layovers": layovers,
        "depart_at": depart_at,
        "arrive_at": arrive_at,
        "first_dep_ts": first_dep_ts,
        "last_arr_ts": last_arr_ts,
    }


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет, завершился ли опрос результатов поиска Aviasales.

    Выдача догружается периодическим опросом (polling). Официального или
    гарантированного флага завершения в структуре данных Aviasales нет,
    поэтому признак является эмпирическим:
    - возвращается True, если среди ответов /search/v3.2/results присутствует
      чанк с chunk_id == 'results' и last_update_timestamp == 0 (число 0;
      эмпирический признак финального ответа, подтверждён на двух запусках);
    - либо проверяется наличие старых флагов завершения is_over=True, completed,
      is_complete, finished в чанках или их метаданных.
    Иначе возвращается False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    for url, body in snapshot.json_payloads:
        if "/search/v3.2/results" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            # На случай битого JSON проверяем текстовое вхождение
            if re.search(r'"is_over"\s*:\s*true', body, re.IGNORECASE):
                return True
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue

            # Эмпирический признак финального ответа: chunk_id == 'results' и last_update_timestamp == 0 (число 0)
            if item.get("chunk_id") == "results":
                ts = item.get("last_update_timestamp")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts == 0:
                    return True

            for key in ("is_over", "completed", "is_complete", "finished"):
                val = item.get(key)
                if val is True or val == "true" or val == 1:
                    return True
                meta = item.get("meta")
                if isinstance(meta, dict):
                    mval = meta.get(key)
                    if mval is True or mval == "true" or mval == 1:
                        return True
    return False


@register_parser("aviasales")
def parse_aviasales(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает XHR-ответы Aviasales (/search/v3.2/results) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Словарь дедупликации: ticket_key -> (priority, Offer)
    # Приоритет определяется порядком прихода ответа в snapshot.json_payloads,
    # а запасным признаком служит last_update_timestamp (ts == 0 означает финальный).
    tickets_by_key: dict[str, tuple[tuple[int, float], Offer]] = {}
    id_to_ticket_key: dict[str, str] = {}
    unkeyed_offers: list[Offer] = []
    total = made = 0  # билетов в выдаче и сколько из них собрано (кривые пропускаем)

    for payload_idx, (url, body) in enumerate(snapshot.json_payloads):
        if "/search/v3.2/results" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        chunks = data if isinstance(data, list) else [data]
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            # Интересуют чанки с результатами
            if (
                chunk.get("chunk_id") != "results"
                and "tickets" not in chunk
                and "cheapest_ticket" not in chunk
                and "filtered_cheapest_ticket" not in chunk
            ):
                continue

            raw_ts = chunk.get("last_update_timestamp")
            if isinstance(raw_ts, bool) or raw_ts is None:
                effective_ts = -1.0
            else:
                try:
                    val = float(raw_ts)
                    effective_ts = float("inf") if val == 0 else val
                except (TypeError, ValueError):
                    effective_ts = -1.0

            current_priority = (payload_idx, effective_ts)

            flight_legs = chunk.get("flight_legs") or []
            airlines = chunk.get("airlines") or {}
            places = chunk.get("places") or {}
            raw_tickets = chunk.get("tickets") or []
            if not isinstance(flight_legs, list):
                continue

            # Добавляем обычные билеты, а также cheapest_ticket и filtered_cheapest_ticket
            all_raw_tickets: list[dict[str, Any]] = []
            if isinstance(raw_tickets, list):
                for t in raw_tickets:
                    if isinstance(t, dict):
                        all_raw_tickets.append(t)
            for key in ("cheapest_ticket", "filtered_cheapest_ticket"):
                val = chunk.get(key)
                if isinstance(val, dict):
                    all_raw_tickets.append(val)

            total += len(all_raw_tickets)
            for t in all_raw_tickets:
                if not isinstance(t, dict):
                    continue
                try:
                    # 1. Поиск самого дешевого предложения (proposal)
                    proposals = t.get("proposals")
                    if not isinstance(proposals, list) or not proposals:
                        continue

                    cheapest_prop = None
                    best_price = None
                    for p in proposals:
                        if not isinstance(p, dict):
                            continue
                        p_price = p.get("price")
                        if not isinstance(p_price, dict):
                            continue
                        raw_val = p_price.get("value")
                        if isinstance(raw_val, bool):
                            continue
                        try:
                            val_int = int(round(float(raw_val)))
                        except (TypeError, ValueError):
                            continue
                        if val_int <= 0:
                            continue
                        if best_price is None or val_int < best_price:
                            best_price = val_int
                            cheapest_prop = p

                    if cheapest_prop is None or best_price is None:
                        continue

                    currency = str(cheapest_prop.get("price", {}).get("currency_code") or query.currency or "RUB").upper()

                    # 2. Сегменты и перелёты
                    segments = t.get("segments")
                    if not isinstance(segments, list):
                        continue

                    is_roundtrip = query.return_date is not None
                    if is_roundtrip:
                        if len(segments) != 2:
                            continue
                        seg0 = segments[0]
                        seg1 = segments[1]
                        info0 = _parse_segment(seg0, flight_legs, airlines, places)
                        info1 = _parse_segment(seg1, flight_legs, airlines, places)
                        if info0 is None or info1 is None:
                            continue
                        back_leg = Leg(
                            airline=info1["airline"],
                            depart_time=info1["depart_time"],
                            arrive_time=info1["arrive_time"],
                            duration=info1["duration"],
                            transfers=info1["transfers"],
                            flight_numbers=info1["flight_numbers"],
                            aircraft=info1["aircraft"],
                            layovers=info1["layovers"],
                            depart_at=info1["depart_at"],
                            arrive_at=info1["arrive_at"],
                        )
                        all_flight_legs = info0["sorted_legs"] + info1["sorted_legs"]
                    else:
                        if len(segments) < 1:
                            continue
                        seg0 = segments[0]
                        info0 = _parse_segment(seg0, flight_legs, airlines, places)
                        if info0 is None:
                            continue
                        info1 = None
                        back_leg = None
                        all_flight_legs = info0["sorted_legs"]

                    # 3. Багаж
                    flight_terms = cheapest_prop.get("flight_terms") or {}
                    b_counts: list[int] = []
                    if isinstance(flight_terms, dict):
                        for f_idx, _ in all_flight_legs:
                            term = flight_terms.get(str(f_idx))
                            if term is None:
                                term = flight_terms.get(f_idx)
                            if isinstance(term, dict):
                                raw_b = term.get("total_baggage_count", 0)
                                if isinstance(raw_b, bool):
                                    b_counts.append(0)
                                else:
                                    try:
                                        b_counts.append(int(raw_b or 0))
                                    except (TypeError, ValueError):
                                        b_counts.append(0)
                            else:
                                b_counts.append(0)

                    has_baggage = bool(b_counts) and all(c > 0 for c in b_counts)
                    baggage = "с багажом" if has_baggage else "без багажа"

                    # 4. Ручная кладь по первому перелёту туда
                    carry_on = None
                    if isinstance(flight_terms, dict):
                        first_f_idx = info0["first_f_idx"]
                        term0 = flight_terms.get(str(first_f_idx))
                        if term0 is None:
                            term0 = flight_terms.get(first_f_idx)
                        if isinstance(term0, dict):
                            hb = term0.get("handbags")
                            if isinstance(hb, dict) and "count" in hb:
                                raw_cnt = hb.get("count")
                                if not isinstance(raw_cnt, bool) and raw_cnt is not None:
                                    try:
                                        cnt = int(raw_cnt)
                                        carry_on = "ручная кладь есть" if cnt > 0 else "ручная кладь нет"
                                    except (TypeError, ValueError):
                                        carry_on = None

                    offer = Offer(
                        site="aviasales",
                        price=best_price,
                        currency=currency,
                        airline=info0["airline"],
                        depart_time=info0["depart_time"],
                        arrive_time=info0["arrive_time"],
                        duration=info0["duration"],
                        transfers=info0["transfers"],
                        baggage=baggage,
                        url=snapshot.url or None,
                        flight_numbers=info0["flight_numbers"],
                        aircraft=info0["aircraft"],
                        layovers=info0["layovers"],
                        depart_at=info0["depart_at"],
                        arrive_at=info0["arrive_at"],
                        carry_on=carry_on,
                        back=back_leg,
                    )

                    # Дедупликация по signature (или clean_id) + маршруту обоих плеч
                    raw_id = t.get("id")
                    clean_id = re.sub(r"_(?:filtered_)?cheapest$", "", str(raw_id)) if raw_id else None
                    sig = t.get("signature")
                    sig_key = str(sig) if sig else clean_id

                    first_dep_ts = info0["first_dep_ts"]
                    first_dep = first_dep_ts if first_dep_ts > 0 else (info0["depart_time"] or "")

                    if is_roundtrip and info1 is not None:
                        back_first_dep_ts = info1["first_dep_ts"]
                        back_dep = back_first_dep_ts if back_first_dep_ts > 0 else (info1["depart_time"] or "")
                        back_last_arr_ts = info1["last_arr_ts"]
                        back_arr = back_last_arr_ts if back_last_arr_ts > 0 else (info1["arrive_time"] or "")
                        back_fn_str = ",".join(info1["flight_numbers"] or [])
                        route_suffix = f"{first_dep}_{back_dep}_{back_arr}_{back_fn_str}"
                    else:
                        route_suffix = f"{first_dep}"

                    ticket_key = f"{sig_key}_{route_suffix}" if sig_key else None
                    id_key = f"{clean_id}_{route_suffix}" if clean_id else None

                    # Если у билета нет signature, но ранее встречался билет с таким же clean_id и маршрутом,
                    # используем уже известный канонический ticket_key (и переносим его при появлении signature).
                    if ticket_key and id_key:
                        if sig and id_key in id_to_ticket_key:
                            prev_canonical = id_to_ticket_key[id_key]
                            if prev_canonical != ticket_key and prev_canonical in tickets_by_key:
                                tickets_by_key[ticket_key] = tickets_by_key.pop(prev_canonical)
                            id_to_ticket_key[id_key] = ticket_key
                        elif not sig and id_key in id_to_ticket_key:
                            ticket_key = id_to_ticket_key[id_key]
                        else:
                            id_to_ticket_key[id_key] = ticket_key

                    if ticket_key:
                        prev = tickets_by_key.get(ticket_key)
                        if (
                            prev is None
                            or current_priority > prev[0]
                            or (current_priority == prev[0] and offer.price <= prev[1].price)
                        ):
                            tickets_by_key[ticket_key] = (current_priority, offer)
                    else:
                        unkeyed_offers.append(offer)
                    made += 1

                except Exception:
                    # Битые билеты пропускаем, не падая
                    continue

    all_offers = [offer for _, offer in tickets_by_key.values()] + unkeyed_offers
    all_offers.sort(key=lambda o: o.price)
    return ParsedOffers(all_offers, total=total, skipped=total - made)


# Привязываем results_complete к функции парсера для удобного вызова из pipeline
parse_aviasales.results_complete = results_complete  # type: ignore[attr-defined]
