"""Парсер живой выдачи Купибилет по XHR/JSON-ответам /frontend_search."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from ..collector import Snapshot
from ..models import Leg, Offer, Query
from . import ParsedOffers, register_parser
from .travelpayouts import format_duration

log = logging.getLogger("avia.parsers.kupibilet")


def _flight_ms(dep_iso: Any) -> int | None:
    """Вычисляет миллисекунды UTC для локальной полуночи даты вылета с учётом часового пояса.

    ms = Date.UTC(год, месяц-1, день по departure_datetime) минус смещение часового пояса
    из departure_datetime в миллисекундах (т.е. локальная полночь даты вылета в поясе
    аэропорта вылета, в мс UTC).
    Формат смещения: ±HH:MM или ±HHMM; если даты/смещения нет — возвращает None.
    """
    if not dep_iso or not isinstance(dep_iso, str):
        return None
    cleaned = dep_iso.strip()
    if not cleaned:
        return None
    try:
        dt = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError) as e:
        log.warning("Купибилет: ошибка парсинга departure_datetime %r: %s", dep_iso, e)
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        log.warning("Купибилет: departure_datetime %r без смещения часового пояса", dep_iso)
        return None
    offset_ms = int(dt.utcoffset().total_seconds() * 1000)
    utc_midnight_ms = int(datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp() * 1000)
    return utc_midnight_ms - offset_ms


def ticket_hash(variant: Any, flights_map: Any) -> str | None:
    """Вычисляет ticket-хеш варианта перелёта Купибилет по рейсам сегментов.

    segmentHash одного рейса = `${ms}-${marketing_carrier}-${number}`.
    tripHash одного направления = segmentHash всех его рейсов (в порядке полёта) через "_".
    ticket = tripHash всех направлений через "_" (туда, потом обратно).
    """
    if not isinstance(variant, dict) or not isinstance(flights_map, dict):
        return None
    segments = variant.get("segments")
    if not isinstance(segments, list) or not segments:
        return None

    trip_hashes: list[str] = []
    for seg_idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            log.warning("Купибилет: неверный формат сегмента %d в варианте", seg_idx)
            return None
        flight_ids = seg.get("flights")
        if not isinstance(flight_ids, list) or not flight_ids:
            log.warning("Купибилет: пустой список рейсов в сегменте %d", seg_idx)
            return None

        seg_hashes: list[str] = []
        for fid in flight_ids:
            fl = flights_map.get(fid)
            if not isinstance(fl, dict):
                log.warning("Купибилет: рейс %r не найден в flights", fid)
                return None

            dep_iso = fl.get("departure_datetime")
            ms = _flight_ms(dep_iso)
            if ms is None:
                return None

            carrier = fl.get("marketing_carrier")
            if not carrier or not isinstance(carrier, str):
                log.warning("Купибилет: отсутствует marketing_carrier в рейсе %r", fid)
                return None
            carrier_str = carrier.strip()
            if not carrier_str:
                log.warning("Купибилет: пустой marketing_carrier в рейсе %r", fid)
                return None

            num = fl.get("number")
            if num is None or num == "":
                num = fl.get("transport_number")
            if num is None or num == "":
                log.warning("Купибилет: отсутствует номер рейса в рейсе %r", fid)
                return None
            num_str = str(num).strip()
            if not num_str:
                log.warning("Купибилет: пустой номер рейса в рейсе %r", fid)
                return None

            seg_hashes.append(f"{ms}-{carrier_str}-{num_str}")

        trip_hashes.append("_".join(seg_hashes))

    if not trip_hashes:
        return None
    return "_".join(trip_hashes)


def build_ticket_url(search_url: str, ticket: str) -> str:
    """Формирует прямую ссылку на конкретный билет Купибилет."""
    if not search_url:
        return ""
    if not ticket:
        return search_url
    params = f"ticket={ticket}&utm_source=direct&utm_medium=search&utm_campaign=tickets&agent=kup747&tag=searchru"
    if search_url.endswith("?") or search_url.endswith("&"):
        return f"{search_url}{params}"
    sep = "&" if "?" in search_url else "?"
    return f"{search_url}{sep}{params}"


def _extract_time(dt_str: Any) -> str | None:
    """Извлекает время в формате HH:MM из строки даты/времени без пересчёта часовых поясов."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    for sep in ("T", " "):
        if sep in dt_str:
            parts = dt_str.split(sep, 1)
            if len(parts) > 1 and len(parts[1]) >= 5:
                return parts[1][:5]
    return dt_str[:5] if len(dt_str) >= 5 else None


def _format_local_iso(dt_str: Any) -> str | None:
    """Преобразует строку даты/времени в формат ISO местного времени вида 'YYYY-MM-DDTHH:MM'."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    cleaned = dt_str.strip()
    if not cleaned:
        return None
    if " " in cleaned:
        cleaned = cleaned.replace(" ", "T", 1)
    if "T" in cleaned:
        d_part, t_part = cleaned.split("T", 1)
        if len(t_part) >= 5:
            return f"{d_part}T{t_part[:5]}"
    return None


def _calculate_duration(dep_iso: Any, arr_iso: Any) -> str | None:
    """Вычисляет длительность перелёта как разность абсолютных моментов с учётом часовых поясов."""
    if not dep_iso or not arr_iso or not isinstance(dep_iso, str) or not isinstance(arr_iso, str):
        return None
    try:
        dep_dt = datetime.fromisoformat(dep_iso)
        arr_dt = datetime.fromisoformat(arr_iso)
    except (ValueError, TypeError):
        return None
    # Без часовой зоны длительность не считаем
    if dep_dt.tzinfo is None or arr_dt.tzinfo is None:
        return None
    diff_sec = (arr_dt - dep_dt).total_seconds()
    if diff_sec <= 0:
        return None
    minutes = int(round(diff_sec / 60))
    return format_duration(minutes)


def _get_city_name(code: Any, anyports: dict[str, Any], cities: dict[str, Any]) -> str:
    """Возвращает русское название города по коду аэропорта или города из anyports/cities."""
    if not code or not isinstance(code, str):
        return ""
    code_str = code.strip()
    if not code_str:
        return ""

    city_code = code_str
    if isinstance(anyports, dict):
        port = anyports.get(code_str)
        if isinstance(port, dict):
            c_code = port.get("city_code")
            if c_code and isinstance(c_code, str):
                city_code = c_code.strip()

    if isinstance(cities, dict):
        for key in (city_code, code_str):
            city = cities.get(key)
            if isinstance(city, dict):
                name = city.get("name")
                if name and isinstance(name, str) and name.strip():
                    return name.strip()
            elif isinstance(city, str) and city.strip():
                return city.strip()

    return code_str


def _format_layover_time(diff_sec: float | int) -> str:
    """Форматирует длительность пересадки вида '2 ч 10 мин', '16 ч' или '45 мин'."""
    if diff_sec <= 0:
        return "0 мин"
    diff_min = int(round(diff_sec / 60))
    h = diff_min // 60
    m = diff_min % 60
    if h > 0 and m > 0:
        return f"{h} ч {m} мин"
    if h > 0:
        return f"{h} ч"
    return f"{m} мин"


def _parse_segment(
    seg: Any,
    flights: dict[str, Any],
    airlines: dict[str, Any],
    aircrafts: dict[str, Any],
    anyports: dict[str, Any],
    cities: dict[str, Any],
) -> dict[str, Any] | None:
    """Разбирает сегмент билета Купибилет и возвращает характеристики плеча или None при ошибке."""
    if not isinstance(seg, dict):
        return None
    flight_ids = seg.get("flights")
    if not isinstance(flight_ids, list) or not flight_ids:
        return None

    seg_flights: list[dict[str, Any]] = []
    for fid in flight_ids:
        fl = flights.get(fid)
        if not isinstance(fl, dict):
            return None
        # Только transport_kind 'airplane' или без этого поля (поезда пропускаем)
        tk = fl.get("transport_kind")
        if tk is not None and str(tk).strip().lower() != "airplane":
            return None
        seg_flights.append(fl)

    if not seg_flights:
        return None

    transfers = len(seg_flights) - 1
    first_fl = seg_flights[0]
    last_fl = seg_flights[-1]

    # Авиакомпания: operating_carrier (или marketing_carrier) первого перелёта из airlines, иначе код
    carrier_code = first_fl.get("operating_carrier") or first_fl.get("marketing_carrier")
    airline: str | None = None
    if carrier_code and isinstance(carrier_code, str):
        airline_info = airlines.get(carrier_code)
        if isinstance(airline_info, dict) and airline_info.get("name"):
            airline = str(airline_info["name"])
        elif isinstance(airline_info, str):
            airline = airline_info
        else:
            airline = carrier_code

    # Местное время вылета и прилёта (HH:MM)
    depart_time = _extract_time(first_fl.get("departure_datetime"))
    arrive_time = _extract_time(last_fl.get("arrival_datetime"))

    # Длительность перелёта с учётом часовых поясов
    duration = _calculate_duration(
        first_fl.get("departure_datetime"),
        last_fl.get("arrival_datetime"),
    )

    # ISO-даты местного времени вылета и прилёта ('YYYY-MM-DDTHH:MM')
    depart_at = _format_local_iso(first_fl.get("departure_datetime"))
    arrive_at = _format_local_iso(last_fl.get("arrival_datetime"))

    # Номера рейсов ("DP 6582" — marketing_carrier + transport_number по каждому перелёту сегмента)
    fn_list: list[str] = []
    for fl in seg_flights:
        c = fl.get("marketing_carrier") or fl.get("operating_carrier")
        n = fl.get("transport_number") or fl.get("number")
        if c and n:
            fn_list.append(f"{c} {n}".strip())
        elif c:
            fn_list.append(str(c).strip())
        elif n:
            fn_list.append(str(n).strip())
    flight_numbers = fn_list if fn_list else None

    # Самолёты (названия из aircrafts через ", ", без дублей; код, если названия нет)
    unique_ac: list[str] = []
    for fl in seg_flights:
        eq = fl.get("equipment")
        if not eq:
            continue
        code = str(eq).strip()
        if not code:
            continue
        ac_name = code
        ac_info = aircrafts.get(code)
        if isinstance(ac_info, dict):
            raw_name = ac_info.get("name")
            if raw_name and isinstance(raw_name, str) and raw_name.strip():
                ac_name = raw_name.strip()
        elif isinstance(ac_info, str) and ac_info.strip():
            ac_name = ac_info.strip()
        if ac_name not in unique_ac:
            unique_ac.append(ac_name)
    aircraft = ", ".join(unique_ac) if unique_ac else None

    # Описание пересадок (город/аэропорт, время ожидания)
    layovers: list[str] | None = None
    if len(seg_flights) > 1:
        lay_list: list[str] = []
        for i in range(len(seg_flights) - 1):
            prev_fl = seg_flights[i]
            next_fl = seg_flights[i + 1]
            arr_code = prev_fl.get("arrival")
            city_name = _get_city_name(arr_code, anyports, cities)

            diff_sec: float | None = None
            arr_iso = prev_fl.get("arrival_datetime")
            dep_iso = next_fl.get("departure_datetime")
            if arr_iso and dep_iso and isinstance(arr_iso, str) and isinstance(dep_iso, str):
                try:
                    arr_dt = datetime.fromisoformat(arr_iso)
                    dep_dt = datetime.fromisoformat(dep_iso)
                    if arr_dt.tzinfo is not None and dep_dt.tzinfo is not None:
                        diff = (dep_dt - arr_dt).total_seconds()
                        if diff >= 0:
                            diff_sec = diff
                except (ValueError, TypeError):
                    diff_sec = None

            if diff_sec is None:
                td = prev_fl.get("transfer_duration")
                if td is not None and not isinstance(td, bool):
                    try:
                        diff_sec = float(td) * 60
                    except (ValueError, TypeError):
                        diff_sec = None

            time_str = _format_layover_time(diff_sec) if diff_sec is not None else ""
            if city_name and time_str:
                lay_list.append(f"{city_name}, {time_str}")
            elif city_name:
                lay_list.append(city_name)
            elif time_str:
                lay_list.append(time_str)

        layovers = lay_list if lay_list else None

    return {
        "airline": airline,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
        "duration": duration,
        "transfers": transfers,
        "flight_numbers": flight_numbers,
        "aircraft": aircraft,
        "layovers": layovers,
        "depart_at": depart_at,
        "arrive_at": arrive_at,
    }


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Купибилет по снимку сетевых ответов.

    Живой сайт Купибилет при поиске делает POST на api-rs-lb.kupibilet.ru/frontend_search.
    За один поиск приходят два ответа: быстрый (около 8 вариантов) и полный
    (около 200 вариантов, через ~3 секунды после первого).

    Эмпирический признак: выдача считается полной (True), если среди ответов,
    содержащих '/frontend_search', есть хотя бы два ответа, или один с числом
    вариантов >= 20 (признак подтверждён на двух запусках). Иначе False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    frontend_search_count = 0
    for url, body in snapshot.json_payloads:
        if "/frontend_search" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        frontend_search_count += 1
        variants = data.get("variants")
        if isinstance(variants, list) and len(variants) >= 20:
            return True

    return frontend_search_count >= 2


@register_parser("kupibilet")
def parse_kupibilet(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы Купибилет (/frontend_search) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Берём ответы, чей URL содержит '/frontend_search'.
    # Если пришло несколько, побеждает ответ с большим числом вариантов (при равенстве более поздний).
    best_payload: dict[str, Any] | None = None
    best_count = -1

    for url, body in snapshot.json_payloads:
        if "/frontend_search" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        variants_raw = data.get("variants")
        count = len(variants_raw) if isinstance(variants_raw, list) else 0
        if count >= best_count:
            best_count = count
            best_payload = data

    if best_payload is None:
        return []

    variants_list = best_payload.get("variants")
    if not isinstance(variants_list, list) or not variants_list:
        return []

    flights = best_payload.get("flights")
    if not isinstance(flights, dict):
        flights = {}

    airlines = best_payload.get("airlines")
    if not isinstance(airlines, dict):
        airlines = {}

    aircrafts = best_payload.get("aircrafts")
    if not isinstance(aircrafts, dict):
        aircrafts = {}

    anyports = best_payload.get("anyports")
    if not isinstance(anyports, dict):
        anyports = {}

    cities = best_payload.get("cities")
    if not isinstance(cities, dict):
        cities = {}

    is_roundtrip = query is not None and query.return_date is not None
    offers_by_key: dict[str, Offer] = {}
    made = 0  # сколько вариантов собрано в билет (кривые пропускаем)

    for variant in variants_list:
        if not isinstance(variant, dict):
            continue
        try:
            price_dict = variant.get("price")
            if not isinstance(price_dict, dict):
                continue
            raw_amount = price_dict.get("amount")
            if raw_amount is None or isinstance(raw_amount, bool):
                continue
            price = int(float(raw_amount))
            if price <= 0:
                continue

            currency = str(price_dict.get("currency") or (query.currency if query is not None else "RUB") or "RUB")

            segments = variant.get("segments")
            if not isinstance(segments, list) or not segments:
                continue

            if is_roundtrip:
                if len(segments) != 2:
                    continue
                info0 = _parse_segment(segments[0], flights, airlines, aircrafts, anyports, cities)
                info1 = _parse_segment(segments[1], flights, airlines, aircrafts, anyports, cities)
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
            else:
                if len(segments) < 1:
                    continue
                info0 = _parse_segment(segments[0], flights, airlines, aircrafts, anyports, cities)
                if info0 is None:
                    continue
                info1 = None
                back_leg = None

            # Багаж (на уровне варианта)
            baggage_info = variant.get("baggage")
            baggage_count = 0
            if isinstance(baggage_info, dict):
                raw_cnt = baggage_info.get("count")
                if raw_cnt is not None and not isinstance(raw_cnt, bool):
                    try:
                        baggage_count = int(raw_cnt)
                    except (ValueError, TypeError):
                        baggage_count = 0
            baggage = "с багажом" if baggage_count > 0 else "без багажа"

            # Ручная кладь (на уровне варианта)
            carry_on = "ручной клади нет"
            hl_info = variant.get("hand_luggage")
            if isinstance(hl_info, dict):
                hl_count = 0.0
                hl_weight = 0.0
                raw_c = hl_info.get("count")
                if raw_c is not None and not isinstance(raw_c, bool):
                    try:
                        hl_count = float(raw_c)
                    except (ValueError, TypeError):
                        hl_count = 0.0
                raw_w = hl_info.get("weight")
                if raw_w is not None and not isinstance(raw_w, bool):
                    try:
                        hl_weight = float(raw_w)
                    except (ValueError, TypeError):
                        hl_weight = 0.0
                if hl_count > 0 or hl_weight > 0:
                    carry_on = "ручная кладь есть"

            offer = Offer(
                site="kupibilet",
                price=price,
                currency=currency,
                airline=info0["airline"],
                depart_time=info0["depart_time"],
                arrive_time=info0["arrive_time"],
                duration=info0["duration"],
                transfers=info0["transfers"],
                baggage=baggage,
                url=None,
                flight_numbers=info0["flight_numbers"],
                aircraft=info0["aircraft"],
                layovers=info0["layovers"],
                depart_at=info0["depart_at"],
                arrive_at=info0["arrive_at"],
                carry_on=carry_on,
                back=back_leg,
                ticket_hash=ticket_hash(variant, flights),
            )

            # Дедупликация: учитываем оба плеча (и багаж), сохраняя самый дешёвый вариант.
            # Разные обратные рейсы не склеиваются.
            raw_id = variant.get("id")
            clean_id = str(raw_id) if raw_id else None

            first_dep = info0["depart_at"] or info0["depart_time"] or ""
            first_fn = ",".join(info0["flight_numbers"] or [])

            if is_roundtrip and info1 is not None:
                back_dep = info1["depart_at"] or info1["depart_time"] or ""
                back_arr = info1["arrive_at"] or info1["arrive_time"] or ""
                back_fn = ",".join(info1["flight_numbers"] or [])
                route_suffix = f"{first_dep}_{first_fn}_{back_dep}_{back_arr}_{back_fn}_{baggage}"
            else:
                route_suffix = f"{first_dep}_{first_fn}_{baggage}"

            dedup_key = f"{clean_id}_{route_suffix}" if clean_id else route_suffix

            if dedup_key in offers_by_key:
                if offer.price < offers_by_key[dedup_key].price:
                    offers_by_key[dedup_key] = offer
            else:
                offers_by_key[dedup_key] = offer
            made += 1
        except Exception:
            # Битые варианты пропускаем, не падая
            continue

    offers = list(offers_by_key.values())
    offers.sort(key=lambda o: o.price)
    return ParsedOffers(offers, total=len(variants_list), skipped=len(variants_list) - made)


# Привязываем results_complete к функции парсера для pipeline.py
parse_kupibilet.results_complete = results_complete  # type: ignore[attr-defined]
