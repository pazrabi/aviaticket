"""Парсер живой выдачи Т-Путешествия (tbank-online.com/travel) по ответам startStreaming.

Схема данных API Т-Путешествия (/api/travel/flight/search/startStreaming):
--------------------------------------------------------------------------
Ответ представляет собой JSON-объект со следующей структурой:
1. `payload`:
   - `offers`: массив предложений перелётов:
     * `price`: объект цены `{"amount": "21522.00", "currency": "RUB"}`;
     * `flights`: массив индексов `[idx_there, idx_back]` (в одну сторону один индекс)
       в массив `payload.flights`;
     * `withBaggage`: булево поле наличия багажа;
     * `segmentsInfo`: вложенный массив информации о багаже и ручной клади по плечам и сегментам;
     * `validatingCarrier`: код валидирующего перевозчика (например, 'S7', 'DP');
     * `offerId`: уникальный идентификатор предложения.
   - `flights`: массив маршрутов:
     * `flightSegments`: сегменты маршрута:
       - `number`: номер рейса (например, '532', '1074');
       - `duration`: длительность сегмента в минутах;
       - `vehicle`: название воздушного судна (например, 'Boeing 737-800');
       - `cabin`: класс обслуживания;
       - `carriers`: `{"operating": "...", "marketing": "..."}`;
       - `departure`: `{"city": "KUF", "airport": "KUF", "time": "2026-12-10T13:50:00+0400"}`;
       - `arrival`: `{"city": "LED", "airport": "LED", "terminal": "1", "time": "2026-12-10T15:20:00+0300"}`.
     * `duration`: полная длительность перелёта по маршруту в минутах.
   - `info`:
     * `carrierNames`: соответствие IATA-кода авиакомпании её названию;
     * `cities`: справочник городов;
     * `airportNames`: справочник названий аэропортов.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any

from ..collector import Snapshot
from ..models import Leg, Offer, Query
from ..offers import compute_offer_id
from . import register_parser
from .travelpayouts import format_duration

logger = logging.getLogger(__name__)


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
        parts = cleaned.split("T", 1)
        if len(parts) > 1 and len(parts[1]) >= 5:
            return f"{parts[0]}T{parts[1][:5]}"
    return None


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


def _build_search_url(query: Query | None, snapshot: Snapshot | None) -> str | None:
    """Формирует прямую ссылку на выдачу поиска Т-Путешествий."""
    if snapshot and snapshot.url and "tbank-online.com/travel/flights" in snapshot.url:
        return snapshot.url
    if query and query.origin and query.destination and query.depart:
        mm_dd = query.depart.strftime("%m-%d")
        if query.return_date:
            mm_dd_ret = query.return_date.strftime("%m-%d")
            return (
                f"https://www.tbank-online.com/travel/flights/multi-way/"
                f"{query.origin}-{query.destination}/{mm_dd}/"
                f"{query.destination}-{query.origin}/{mm_dd_ret}/"
                f"?adults={query.adults}&children=0&infants=0&cabin=Y&composite=0"
            )
        return (
            f"https://www.tbank-online.com/travel/flights/one-way/"
            f"{query.origin}-{query.destination}/{mm_dd}/"
            f"?adults={query.adults}&children=0&infants=0&cabin=Y&composite=0"
        )
    if snapshot and snapshot.url:
        return snapshot.url
    return None


def _parse_flight_leg(
    flight: dict[str, Any],
    carrier_names: dict[str, Any],
    cities: dict[str, Any],
    airport_names: dict[str, Any],
    validating_carrier: str | None = None,
) -> dict[str, Any] | None:
    """Разбирает объект маршрута (flight) в словарь полей плеча."""
    segs = flight.get("flightSegments")
    if not isinstance(segs, list) or not segs:
        return None

    first_seg = segs[0]
    last_seg = segs[-1]

    # Маркетинговый перевозчик первого сегмента с фоллбэком на оператора / валидатора
    carriers = first_seg.get("carriers") if isinstance(first_seg.get("carriers"), dict) else {}
    carrier_code = (
        carriers.get("marketing")
        or carriers.get("operating")
        or validating_carrier
    )
    airline = None
    if carrier_code:
        airline = carrier_names.get(carrier_code) or str(carrier_code)

    dep_obj = first_seg.get("departure") if isinstance(first_seg.get("departure"), dict) else {}
    arr_obj = last_seg.get("arrival") if isinstance(last_seg.get("arrival"), dict) else {}

    dep_time_str = dep_obj.get("time")
    arr_time_str = arr_obj.get("time")

    depart_time = _extract_time(dep_time_str)
    arrive_time = _extract_time(arr_time_str)
    depart_at = _format_local_iso(dep_time_str)
    arrive_at = _format_local_iso(arr_time_str)

    # Длительность перелёта (из duration или разности departure/arrival с таймзонами)
    dur_minutes = flight.get("duration")
    duration = None
    if isinstance(dur_minutes, (int, float)) and dur_minutes > 0:
        duration = format_duration(int(dur_minutes))
    elif dep_time_str and arr_time_str:
        try:
            d_dt = datetime.fromisoformat(dep_time_str)
            a_dt = datetime.fromisoformat(arr_time_str)
            diff_sec = (a_dt - d_dt).total_seconds()
            if diff_sec > 0:
                duration = format_duration(int(round(diff_sec / 60)))
        except (ValueError, TypeError):
            pass

    # Число пересадок = количество сегментов - 1
    transfers = max(0, len(segs) - 1)

    # Номера рейсов и самолёты
    flight_numbers: list[str] = []
    vehicles: list[str] = []
    for s in segs:
        num = str(s.get("number", "")).strip()
        s_carriers = s.get("carriers") if isinstance(s.get("carriers"), dict) else {}
        code = s_carriers.get("marketing") or s_carriers.get("operating") or carrier_code
        if code and num:
            if not num.startswith(code):
                flight_numbers.append(f"{code} {num}")
            else:
                flight_numbers.append(num)
        elif num:
            flight_numbers.append(num)

        veh = s.get("vehicle")
        if veh and isinstance(veh, str) and veh.strip():
            clean_v = veh.strip()
            if clean_v not in vehicles:
                vehicles.append(clean_v)

    aircraft = ", ".join(vehicles) if vehicles else None

    # Пересадки (город/аэропорт и длительность с учётом часовых поясов)
    layovers: list[str] | None = None
    if len(segs) > 1:
        lay_list: list[str] = []
        for i in range(len(segs) - 1):
            prev_arr = segs[i].get("arrival") if isinstance(segs[i].get("arrival"), dict) else {}
            next_dep = segs[i + 1].get("departure") if isinstance(segs[i + 1].get("departure"), dict) else {}

            c_code = prev_arr.get("city")
            a_code = prev_arr.get("airport")
            city_name = ""
            if c_code and c_code in cities:
                c_data = cities[c_code]
                if isinstance(c_data, dict):
                    city_name = c_data.get("name", "")
                elif isinstance(c_data, str):
                    city_name = c_data
            if not city_name and a_code and a_code in airport_names:
                city_name = str(airport_names[a_code])
            if not city_name:
                city_name = c_code or a_code or ""

            prev_time = prev_arr.get("time")
            next_time = next_dep.get("time")
            t_str = ""
            if prev_time and next_time:
                try:
                    p_dt = datetime.fromisoformat(prev_time)
                    n_dt = datetime.fromisoformat(next_time)
                    diff_sec = (n_dt - p_dt).total_seconds()
                    if diff_sec > 0:
                        t_str = _format_layover_time(diff_sec)
                except (ValueError, TypeError):
                    pass
            if city_name and t_str:
                lay_list.append(f"{city_name}, {t_str}")
            elif city_name:
                lay_list.append(city_name)
            elif t_str:
                lay_list.append(t_str)
        if lay_list:
            layovers = lay_list

    return {
        "airline": airline,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
        "duration": duration,
        "transfers": transfers,
        "flight_numbers": flight_numbers if flight_numbers else None,
        "aircraft": aircraft,
        "layovers": layovers,
        "depart_at": depart_at,
        "arrive_at": arrive_at,
    }


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту выдачи Т-Путешествий (ответ startStreaming получен и содержит офферы)."""
    if not snapshot or not snapshot.json_payloads:
        return False
    for url, body in snapshot.json_payloads:
        if "startStreaming" not in url:
            continue
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                p = data.get("payload")
                if isinstance(p, dict) and "offers" in p:
                    return True
        except Exception:
            continue
    return False


@register_parser("ttravel")
def parse_ttravel(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает сетевые JSON-ответы startStreaming Т-Путешествий в список моделей Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    streaming_payloads = [
        (url, body) for url, body in snapshot.json_payloads
        if "startStreaming" in url
    ]
    if not streaming_payloads:
        return []

    url = _build_search_url(query, snapshot)
    offers: list[Offer] = []

    for _, body in streaming_payloads:
        try:
            data = json.loads(body)
        except Exception as exc:
            logger.warning("Ошибка разбора JSON startStreaming: %s", exc)
            continue

        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            continue

        offers_raw = payload.get("offers")
        flights_raw = payload.get("flights")
        info = payload.get("info") or {}
        if not isinstance(offers_raw, list) or not isinstance(flights_raw, list):
            continue

        carrier_names = info.get("carrierNames") if isinstance(info.get("carrierNames"), dict) else {}
        cities = info.get("cities") if isinstance(info.get("cities"), dict) else {}
        airport_names = info.get("airportNames") if isinstance(info.get("airportNames"), dict) else {}

        is_roundtrip = bool(query and query.return_date)

        for offer_raw in offers_raw:
            if not isinstance(offer_raw, dict):
                continue
            try:
                price_obj = offer_raw.get("price")
                if not isinstance(price_obj, dict):
                    continue
                amount_raw = price_obj.get("amount")
                if amount_raw is None:
                    continue
                try:
                    price_val = int(float(amount_raw))
                except (ValueError, TypeError):
                    continue
                if price_val <= 0:
                    continue
                currency = price_obj.get("currency", "RUB")

                flight_indices = offer_raw.get("flights")
                if not isinstance(flight_indices, list) or not flight_indices:
                    continue

                if is_roundtrip:
                    if len(flight_indices) < 2:
                        continue
                    idx_there = flight_indices[0]
                    idx_back = flight_indices[1]
                    if not isinstance(idx_there, int) or idx_there < 0 or idx_there >= len(flights_raw):
                        continue
                    if not isinstance(idx_back, int) or idx_back < 0 or idx_back >= len(flights_raw):
                        continue
                    flight_there = flights_raw[idx_there]
                    flight_back = flights_raw[idx_back]
                else:
                    idx_there = flight_indices[0]
                    if not isinstance(idx_there, int) or idx_there < 0 or idx_there >= len(flights_raw):
                        continue
                    flight_there = flights_raw[idx_there]
                    flight_back = None

                val_carrier = offer_raw.get("validatingCarrier")
                there_info = _parse_flight_leg(flight_there, carrier_names, cities, airport_names, val_carrier)
                if not there_info:
                    continue

                back_leg: Leg | None = None
                if is_roundtrip and flight_back:
                    back_info = _parse_flight_leg(flight_back, carrier_names, cities, airport_names, val_carrier)
                    if not back_info:
                        continue
                    back_leg = Leg(
                        airline=back_info["airline"],
                        depart_time=back_info["depart_time"],
                        arrive_time=back_info["arrive_time"],
                        duration=back_info["duration"],
                        transfers=back_info["transfers"],
                        flight_numbers=back_info["flight_numbers"],
                        aircraft=back_info["aircraft"],
                        layovers=back_info["layovers"],
                        depart_at=back_info["depart_at"],
                        arrive_at=back_info["arrive_at"],
                    )

                # Багаж
                with_baggage = offer_raw.get("withBaggage")
                if with_baggage is True:
                    baggage = "с багажом"
                elif with_baggage is False:
                    baggage = "без багажа"
                else:
                    has_b = False
                    for leg_segs in offer_raw.get("segmentsInfo", []):
                        if isinstance(leg_segs, list):
                            for s_info in leg_segs:
                                if isinstance(s_info, dict):
                                    for b in s_info.get("baggage", []):
                                        if isinstance(b, dict) and b.get("count", 0) > 0:
                                            b_inner = b.get("baggage", {})
                                            if isinstance(b_inner, dict) and (b_inner.get("amount", 0) or 0) > 0:
                                                has_b = True
                    baggage = "с багажом" if has_b else "без багажа"

                # Ручная кладь
                carry_on = None
                has_hb = False
                for leg_segs in offer_raw.get("segmentsInfo", []):
                    if isinstance(leg_segs, list):
                        for s_info in leg_segs:
                            if isinstance(s_info, dict):
                                for hb in s_info.get("handBaggage", []):
                                    if isinstance(hb, dict) and hb.get("count", 0) > 0:
                                        has_hb = True
                if has_hb:
                    carry_on = "ручная кладь есть"

                offer = Offer(
                    site="ttravel",
                    price=price_val,
                    currency=currency,
                    airline=there_info["airline"],
                    depart_time=there_info["depart_time"],
                    arrive_time=there_info["arrive_time"],
                    duration=there_info["duration"],
                    transfers=there_info["transfers"],
                    baggage=baggage,
                    carry_on=carry_on,
                    url=url,
                    flight_numbers=there_info["flight_numbers"],
                    aircraft=there_info["aircraft"],
                    layovers=there_info["layovers"],
                    depart_at=there_info["depart_at"],
                    arrive_at=there_info["arrive_at"],
                    back=back_leg,
                )
                offer.offer_id = compute_offer_id(offer)
                offers.append(offer)
            except Exception as exc:
                logger.warning("Ошибка обработки оффера ttravel: %s", exc)
                continue

    offers.sort(key=lambda o: o.price)
    return offers


parse_ttravel.results_complete = results_complete  # type: ignore[attr-defined]
