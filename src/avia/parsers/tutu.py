"""Парсер живой выдачи Туту (avia.tutu.ru) по XHR/JSON-ответам /avia/offers/v2.

Схема данных API Туту (/avia/offers/v2):
----------------------------------------
Страница avia.tutu.ru отправляет POST-запрос на https://offers-api.tutu.ru/avia/offers/v2.
Ответ представляет собой JSON-массив из одного объекта-чанка со следующей структурой:
1. `summary`:
   - `offersCurrentAmount`: число предложений в текущем снимке/выдаче (например, 65, 87);
   - `offersTotalAmount`: общее число найденных предложений;
   - `currentMinPrice`, `totalMinPrice`: минимальная цена по всей выборке;
   - `avia`: объект со сводкой (`minPrice`, `direct`, `withLuggage`).
2. `pagination`:
   - `limit`: ограничение числа предложений на страницу (по умолчанию 10);
   - `offset`: смещение пагинации.
3. `offers`:
   - `actual`: словарь предложений `{<offer_id>: offer_data}`, где каждое предложение содержит:
     * `id`: строковый идентификатор предложения;
     * `pinLink`: прямая ссылка на предложение вида https://avia.tutu.ru/offers/?...&pin=...;
     * `routeIds`: массив идентификаторов маршрутов, ссылающихся на `dictionary.common.routes`;
     * `offerVariants`: массив тарифных вариантов для данного перелёта.
4. `dictionary`:
   - `common.routes`: словарь маршрутов `{<route_id>: {id, segmentIds, transferIds}}`.
     Число пересадок маршрута определяется как `len(transferIds)` либо `len(segmentIds) - 1`.
   - `common.segments`: словарь сегментов перелёта `{<segment_id>: {...}}`:
     * `departureDateTime`: время вылета в ISO-формате с таймзоной (например, '2026-12-12T12:35:00+03:00');
     * `arrivalDateTime`: время прилёта в ISO-формате с таймзоной (например, '2026-12-13T00:50:00+05:00');
     * `duration`: длительность сегмента в минутах;
     * `carriers`: массив перевозчиков `[{id: str, type: 'marketing' | 'operating'}]`;
     * `voyageNumber`: номер рейса (например, 'DV-1010');
     * `vehicleId`: идентификатор борта из `dictionary.avia.vehicles`;
     * `departureGeoPointId`, `arrivalGeoPointId`: гео-точки из `dictionary.avia.points` / `dictionary.common.cities`.
   - `common.carriers`: справочник перевозчиков `{<carrier_id>: {name, displaySettings, uri}}`.
   - `common.fareApplications`: справочник применений тарифов `{<fare_app_hash>: {segmentConditions, segmentHash, serviceClass, ...}}`.
   - `avia.conditions`: справочник условий тарифа `{<condition_id>: {baggageKg: {defined, value}, baggagePc: {defined, value}, cabinBaggageKg, cabinBaggagePc, fareFamily, ...}}`.
     Включение багажа определяется по полям `baggageKg.value > 0` или `baggagePc.value > 0`.
   - `avia.vehicles`: справочник воздушных судов `{<vehicle_id>: {name}}`.
   - `avia.points`: справочник аэропортов и гео-точек `{<point_id>: {name, cityId, airportCode}}`.
   - `common.cities`: справочник городов `{<city_id>: {name: {nominative, ...}}}`.

Режим туда-обратно (roundtrip) и ценообразование:
-------------------------------------------------
При поиске туда-обратно (когда задан `query.return_date`) каждое предложение в `offers.actual`
содержит ровно два идентификатора маршрутов в `routeIds`: `[маршрут туда, маршрут обратно]`.
Цена в каждом тарифном варианте `offerVariants` (поле `price.value.amount // price.value.fraction`)
является ОБЩЕЙ (полной) стоимостью за оба плеча (туда и обратно). Это подтверждается
сопоставлением с полем `offerHash.Price` (содержит ту же итоговую сумму) и `OriginalHash`,
в котором объединены тарифные хэши обоих маршрутов. Копейки (`fraction: 100`) учитываются
целочисленным делением `amount // fraction`. Дополнительного суммирования цен между плечами не требуется;
парсер выбирает минимальную общую стоимость среди `offerVariants`.
Норма багажа: в режиме туда-обратно тариф считается «с багажом» только если условия ВСЕХ сегментов
обоих маршрутов (туда и обратно) имеют `baggageKg > 0` или `baggagePc > 0`. Иначе — «без багажа».
В одностороннем режиме сохраняется исходная логика (достаточно одного сегмента).

Ограничение по объёму выдачи:
------------------------------
API Туту возвращает только одну страницу (limit=10 предложений), тогда как глобальный минимум
по всему пулу найденных вариантов содержится в `summary.totalMinPrice` / `summary.avia.minPrice`.
Если среди разобранных предложений (до 10 штук на страницу) нет цены, равной `totalMinPrice`,
это нормальное поведение из-за пагинации; парсер извлекает и возвращает все фактически присутствующие
в ответе предложения.

Эмпирический признак полноты выдачи (results_complete):
------------------------------------------------------
Поиск Туту подгружает предложения асинхронно. На практике при поиске возвращается сначала
промежуточный чанк (около 65 предложений), а затем финальный (около 87 предложений).
Выдача считается полной (`results_complete(snapshot) == True`), если среди ответов '/avia/offers/v2'
присутствует хотя бы два ответа, либо один с `offersCurrentAmount >= 20`. Иначе `False`.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any

from ..collector import Snapshot
from ..models import Leg, Offer, Query
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
        d_part, t_part = cleaned.split("T", 1)
        if len(t_part) >= 5:
            return f"{d_part}T{t_part[:5]}"
    return None


def _calculate_duration(dep_iso: Any, arr_iso: Any, fallback_duration: Any = None) -> str | None:
    """Вычисляет длительность перелёта как разность абсолютных моментов или использует запасное поле длительности."""
    if isinstance(dep_iso, str) and isinstance(arr_iso, str):
        try:
            dep_dt = datetime.fromisoformat(dep_iso)
            arr_dt = datetime.fromisoformat(arr_iso)
            if dep_dt.tzinfo is not None and arr_dt.tzinfo is not None:
                diff_sec = (arr_dt - dep_dt).total_seconds()
                if diff_sec > 0:
                    minutes = int(round(diff_sec / 60))
                    return format_duration(minutes)
        except (ValueError, TypeError):
            pass
    if fallback_duration is not None and not isinstance(fallback_duration, bool):
        try:
            return format_duration(int(fallback_duration))
        except (ValueError, TypeError):
            pass
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


def _get_airline_name(seg: dict[str, Any], carriers_dict: dict[str, Any]) -> str | None:
    """Возвращает название авиакомпании из справочника common.carriers, иначе код."""
    carriers_list = seg.get("carriers")
    carrier_ref = None
    if isinstance(carriers_list, list) and carriers_list:
        for c in carriers_list:
            if isinstance(c, dict) and c.get("type") == "operating":
                carrier_ref = c
                break
        if carrier_ref is None:
            for c in carriers_list:
                if isinstance(c, dict):
                    carrier_ref = c
                    break

    carrier_id = str(carrier_ref.get("id")) if carrier_ref and carrier_ref.get("id") is not None else None
    if carrier_id and carrier_id in carriers_dict:
        c_info = carriers_dict[carrier_id]
        if isinstance(c_info, dict):
            name = c_info.get("name")
            if name:
                return str(name)
            code = c_info.get("code") or c_info.get("iata")
            if code:
                return str(code)

    voyage = seg.get("voyageNumber")
    if isinstance(voyage, str) and "-" in voyage:
        return voyage.split("-", 1)[0]
    if carrier_id:
        return str(carrier_id)
    return None


def _get_city_name(
    geo_point_id: Any,
    points_dict: dict[str, Any],
    cities_dict: dict[str, Any],
) -> str:
    """Возвращает название города пересадки по гео-точке или городу из справочников."""
    if geo_point_id is None:
        return ""
    g_key = str(geo_point_id)
    # Сначала проверяем dictionary.avia.points
    pt = points_dict.get(g_key)
    if isinstance(pt, dict):
        city_id = pt.get("cityId")
        if city_id is not None:
            c_info = cities_dict.get(str(city_id))
            if isinstance(c_info, dict):
                name = c_info.get("name")
                if isinstance(name, dict):
                    nom = name.get("nominative")
                    if nom and isinstance(nom, str):
                        return nom.strip()
                elif isinstance(name, str) and name.strip():
                    return name.strip()
        p_name = pt.get("name")
        if isinstance(p_name, dict):
            nom = p_name.get("nominative")
            if nom and isinstance(nom, str):
                return nom.strip()
        elif isinstance(p_name, str) and p_name.strip():
            return p_name.strip()

    # Проверяем напрямую dictionary.common.cities
    c_info = cities_dict.get(g_key)
    if isinstance(c_info, dict):
        name = c_info.get("name")
        if isinstance(name, dict):
            nom = name.get("nominative")
            if nom and isinstance(nom, str):
                return nom.strip()
        elif isinstance(name, str) and name.strip():
            return name.strip()

    return ""


def _segment_has_baggage(
    seg_id: str,
    fare_applications: dict[str, Any],
    fare_apps_dict: dict[str, Any],
    conditions_dict: dict[str, Any],
) -> bool:
    """Проверяет наличие багажа (baggageKg > 0 или baggagePc > 0) для конкретного сегмента."""
    hashes = fare_applications.get(seg_id)
    if not hashes:
        return False
    hash_list = hashes if isinstance(hashes, list) else [hashes]
    for fh in hash_list:
        if not isinstance(fh, str):
            continue
        fa = fare_apps_dict.get(fh)
        if not isinstance(fa, dict):
            continue
        cond_id = fa.get("segmentConditions")
        if not cond_id or not isinstance(cond_id, str):
            continue
        cond = conditions_dict.get(cond_id)
        if not isinstance(cond, dict):
            continue

        for field_name in ("baggageKg", "baggagePc"):
            b_val = cond.get(field_name)
            val = None
            if isinstance(b_val, dict):
                val = b_val.get("value")
            elif isinstance(b_val, (int, float)) and not isinstance(b_val, bool):
                val = b_val
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return True
    return False


def _has_baggage(
    variant: dict[str, Any],
    fare_apps_dict: dict[str, Any],
    conditions_dict: dict[str, Any],
    required_segment_ids: list[str] | None = None,
) -> bool:
    """Определяет, включён ли багаж в тариф.

    В режиме туда-обратно (когда передан required_segment_ids) багаж считается включённым
    только если условия ВСЕХ сегментов обоих плеч дают baggageKg/baggagePc > 0.
    В одностороннем режиме сохраняется прежняя логика: True, если хотя бы один сегмент с багажом.
    """
    fare_applications = variant.get("fareApplications")
    if not isinstance(fare_applications, dict):
        return False

    if required_segment_ids is not None:
        if not required_segment_ids:
            return False
        return all(
            _segment_has_baggage(sid, fare_applications, fare_apps_dict, conditions_dict)
            for sid in required_segment_ids
        )

    for _seg_id, hashes in fare_applications.items():
        hash_list = hashes if isinstance(hashes, list) else [hashes]
        for fh in hash_list:
            if not isinstance(fh, str):
                continue
            fa = fare_apps_dict.get(fh)
            if not isinstance(fa, dict):
                continue
            cond_id = fa.get("segmentConditions")
            if not cond_id or not isinstance(cond_id, str):
                continue
            cond = conditions_dict.get(cond_id)
            if not isinstance(cond, dict):
                continue

            for field_name in ("baggageKg", "baggagePc"):
                b_val = cond.get(field_name)
                val = None
                if isinstance(b_val, dict):
                    val = b_val.get("value")
                elif isinstance(b_val, (int, float)) and not isinstance(b_val, bool):
                    val = b_val
                if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                    return True

    return False


def _get_carry_on(
    variant: dict[str, Any],
    first_seg_id: str | None,
    fare_apps_dict: dict[str, Any],
    conditions_dict: dict[str, Any],
) -> str | None:
    """Определяет наличие ручной клади: 'ручная кладь есть' / 'ручная кладь нет' при явных данных, иначе None."""
    fare_applications = variant.get("fareApplications")
    if not isinstance(fare_applications, dict):
        return None

    target_hashes: list[str] = []
    if first_seg_id and first_seg_id in fare_applications:
        val = fare_applications[first_seg_id]
        target_hashes = val if isinstance(val, list) else [val]
    else:
        for hashes in fare_applications.values():
            target_hashes = hashes if isinstance(hashes, list) else [hashes]
            if target_hashes:
                break

    for fh in target_hashes:
        if not isinstance(fh, str):
            continue
        fa = fare_apps_dict.get(fh)
        if not isinstance(fa, dict):
            continue
        cond_id = fa.get("segmentConditions")
        if not cond_id or not isinstance(cond_id, str):
            continue
        cond = conditions_dict.get(cond_id)
        if not isinstance(cond, dict):
            continue

        pc_info = cond.get("cabinBaggagePc")
        kg_info = cond.get("cabinBaggageKg")

        has_explicit = False
        has_carry = False

        if isinstance(pc_info, dict) and pc_info.get("defined") is True:
            has_explicit = True
            v = pc_info.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                has_carry = True

        if isinstance(kg_info, dict) and kg_info.get("defined") is True:
            has_explicit = True
            v = kg_info.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                has_carry = True

        if has_explicit:
            return "ручная кладь есть" if has_carry else "ручная кладь нет"

    return None


def _parse_route_leg(
    route_id: str,
    routes_dict: dict[str, Any],
    segments_dict: dict[str, Any],
    carriers_dict: dict[str, Any],
    vehicles_dict: dict[str, Any],
    points_dict: dict[str, Any],
    cities_dict: dict[str, Any],
) -> dict[str, Any] | None:
    """Разбирает маршрут (routeId) и возвращает словарь с характеристиками плеча перелёта."""
    if not route_id or not isinstance(route_id, str):
        return None
    route = routes_dict.get(route_id)
    if not isinstance(route, dict):
        return None
    segment_ids = route.get("segmentIds")
    if not isinstance(segment_ids, list) or not segment_ids:
        return None

    segs: list[dict[str, Any]] = []
    for sid in segment_ids:
        s = segments_dict.get(sid)
        if not isinstance(s, dict):
            return None
        segs.append(s)

    first_seg = segs[0]
    last_seg = segs[-1]

    # Число пересадок
    transfer_ids = route.get("transferIds")
    if isinstance(transfer_ids, list):
        transfers = len(transfer_ids)
    else:
        transfers = max(0, len(segment_ids) - 1)

    # Авиакомпания перевозчика первого сегмента
    airline = _get_airline_name(first_seg, carriers_dict)

    # Время вылета и прилёта (HH:MM)
    depart_time = _extract_time(first_seg.get("departureDateTime"))
    arrive_time = _extract_time(last_seg.get("arrivalDateTime"))

    # Длительность перелёта
    duration = _calculate_duration(
        first_seg.get("departureDateTime"),
        last_seg.get("arrivalDateTime"),
        fallback_duration=first_seg.get("duration"),
    )

    # Даты вылета и прилёта в ISO местного времени ("2026-12-16T10:15")
    depart_at = _format_local_iso(first_seg.get("departureDateTime"))
    arrive_at = _format_local_iso(last_seg.get("arrivalDateTime"))

    # Номера рейсов ("DV-1010" -> "DV 1010")
    fn_list: list[str] = []
    for s in segs:
        voyage = s.get("voyageNumber")
        if isinstance(voyage, str) and voyage.strip():
            fn_list.append(voyage.replace("-", " ").strip())
    flight_numbers = fn_list if fn_list else None

    # Самолёты из справочника vehicles
    ac_list: list[str] = []
    for s in segs:
        vid = s.get("vehicleId")
        if vid is not None and vid != 0:
            veh = vehicles_dict.get(str(vid)) or vehicles_dict.get(vid)
            if isinstance(veh, dict):
                v_name = veh.get("name")
                if v_name and isinstance(v_name, str):
                    clean_vn = v_name.strip()
                    if clean_vn and clean_vn not in ac_list:
                        ac_list.append(clean_vn)
    aircraft = ", ".join(ac_list) if ac_list else None

    # Описание пересадок (город/аэропорт, время ожидания)
    layovers: list[str] | None = None
    if len(segs) > 1:
        lay_list: list[str] = []
        for i in range(len(segs) - 1):
            p_s = segs[i]
            n_s = segs[i + 1]
            c_name = _get_city_name(p_s.get("arrivalGeoPointId"), points_dict, cities_dict)
            try:
                dt_arr = datetime.fromisoformat(p_s["arrivalDateTime"])
                dt_dep = datetime.fromisoformat(n_s["departureDateTime"])
                diff_sec = (dt_dep - dt_arr).total_seconds()
            except (ValueError, TypeError, KeyError):
                diff_sec = -1
            if diff_sec > 0:
                t_str = _format_layover_time(diff_sec)
                lay_str = f"{c_name}, {t_str}" if c_name else t_str
            elif c_name:
                lay_str = c_name
            else:
                lay_str = ""
            if lay_str:
                lay_list.append(lay_str)
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
        "segment_ids": segment_ids,
        "first_seg_id": segment_ids[0],
    }


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Туту по снимку сетевых ответов.

    Выдача считается полной (True), если среди ответов '/avia/offers/v2' есть хотя бы два,
    или один с offersCurrentAmount >= 20, или один, где offersCurrentAmount равен offersTotalAmount
    (показано всё, что нашлось). Иначе False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    v2_count = 0
    for url, body in snapshot.json_payloads:
        if "/avia/offers/v2" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        if isinstance(data, list) and data:
            chunk = data[0]
        elif isinstance(data, dict):
            chunk = data
        else:
            continue

        if not isinstance(chunk, dict):
            continue

        v2_count += 1
        summary = chunk.get("summary")
        if isinstance(summary, dict):
            raw_amt = summary.get("offersCurrentAmount")
            if isinstance(raw_amt, (int, float)) and not isinstance(raw_amt, bool) and raw_amt >= 20:
                return True
            raw_total = summary.get("offersTotalAmount")
            if (
                isinstance(raw_amt, (int, float)) and not isinstance(raw_amt, bool)
                and isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool)
                and raw_amt > 0 and raw_amt == raw_total
            ):
                return True

    return v2_count >= 2


def merge_tutu_chunks(chunk1: dict[str, Any], chunk2: dict[str, Any]) -> dict[str, Any]:
    """Объединяет два чанка выдачи Туту (/avia/offers/v2), дедуплицируя офферы по id."""
    merged = dict(chunk1)

    # 1. Summary: берем из chunk2 или chunk1
    s1 = chunk1.get("summary") if isinstance(chunk1.get("summary"), dict) else {}
    s2 = chunk2.get("summary") if isinstance(chunk2.get("summary"), dict) else {}
    merged_summary = dict(s1)
    merged_summary.update(s2)
    merged["summary"] = merged_summary

    # 2. Dictionary: объединяем секции common и avia
    d1 = chunk1.get("dictionary") if isinstance(chunk1.get("dictionary"), dict) else {}
    d2 = chunk2.get("dictionary") if isinstance(chunk2.get("dictionary"), dict) else {}
    merged_dict: dict[str, Any] = {}

    for section in ("common", "avia"):
        sec1 = d1.get(section) if isinstance(d1.get(section), dict) else {}
        sec2 = d2.get(section) if isinstance(d2.get(section), dict) else {}
        merged_sec = dict(sec1)
        for tbl_name, tbl_val in sec2.items():
            if isinstance(tbl_val, dict) and isinstance(merged_sec.get(tbl_name), dict):
                merged_tbl = dict(merged_sec[tbl_name])
                merged_tbl.update(tbl_val)
                merged_sec[tbl_name] = merged_tbl
            else:
                merged_sec[tbl_name] = tbl_val
        merged_dict[section] = merged_sec
    merged["dictionary"] = merged_dict

    # 3. Offers: actual
    o1 = (chunk1.get("offers") or {}).get("actual") if isinstance(chunk1.get("offers"), dict) else {}
    o2 = (chunk2.get("offers") or {}).get("actual") if isinstance(chunk2.get("offers"), dict) else {}
    merged_actual = dict(o1) if isinstance(o1, dict) else {}
    if isinstance(o2, dict):
        for k, v in o2.items():
            if isinstance(v, dict):
                off_id = str(v.get("id") or k)
                # Если оффер уже есть по ID, не удваиваем (сохраняем оффер)
                merged_actual[off_id] = v
            else:
                merged_actual[k] = v
    merged["offers"] = {"actual": merged_actual}

    return merged


def merge_tutu_payloads(payload1: str | dict | list, payload2: str | dict | list) -> dict[str, Any]:
    """Объединяет два JSON-ответа (в виде строки, dict или list) Туту."""
    def _unwrap(p: str | dict | list) -> dict[str, Any]:
        if isinstance(p, str):
            p = json.loads(p)
        if isinstance(p, list) and p and isinstance(p[0], dict):
            return p[0]
        if isinstance(p, dict):
            return p
        return {}

    c1 = _unwrap(payload1)
    c2 = _unwrap(payload2)
    return merge_tutu_chunks(c1, c2)


@register_parser("tutu")
def parse_tutu(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы Туту (/avia/offers/v2) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Берём ответы, чей URL содержит '/avia/offers/v2'.
    # Ответы с replayed=True представляют собой вторую страницу (результат replay_post).
    # Для обычных ответов побеждает ответ с большим summary.offersCurrentAmount (при равенстве более поздний).
    normal_chunks: list[tuple[int, dict[str, Any]]] = []
    replayed_chunks: list[dict[str, Any]] = []

    for url, body in snapshot.json_payloads:
        if "/avia/offers/v2" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        if isinstance(data, list) and data:
            chunk = data[0]
        elif isinstance(data, dict):
            chunk = data
        else:
            continue

        if not isinstance(chunk, dict):
            continue

        is_replayed = bool(
            chunk.get("replayed", False)
            or (isinstance(data, dict) and data.get("replayed", False))
        )

        if is_replayed:
            replayed_chunks.append(chunk)
        else:
            summary = chunk.get("summary")
            amount = -1
            if isinstance(summary, dict):
                raw_amt = summary.get("offersCurrentAmount")
                if isinstance(raw_amt, (int, float)) and not isinstance(raw_amt, bool):
                    amount = int(raw_amt)
            normal_chunks.append((amount, chunk))

    best_chunk: dict[str, Any] | None = None
    best_amount = -1
    for amount, chunk in normal_chunks:
        if amount >= best_amount:
            best_amount = amount
            best_chunk = chunk

    if not replayed_chunks:
        if best_chunk is None:
            return []
        final_chunk = best_chunk
    else:
        if best_chunk is not None:
            final_chunk = best_chunk
            for r_chunk in replayed_chunks:
                try:
                    final_chunk = merge_tutu_chunks(final_chunk, r_chunk)
                except Exception as exc:
                    logger.warning("Ошибка слияния replayed-ответа Туту: %s, откат на базовую порцию", exc)
        else:
            final_chunk = replayed_chunks[0]
            for r_chunk in replayed_chunks[1:]:
                try:
                    final_chunk = merge_tutu_chunks(final_chunk, r_chunk)
                except Exception as exc:
                    logger.warning("Ошибка слияния replayed-ответов Туту: %s", exc)

    if final_chunk is None:
        if best_chunk is not None:
            final_chunk = best_chunk
        else:
            return []

    offers_data = final_chunk.get("offers")
    if not isinstance(offers_data, dict):
        return []

    actual_offers = offers_data.get("actual")
    if not isinstance(actual_offers, dict) or not actual_offers:
        return []

    dictionary = final_chunk.get("dictionary")
    if not isinstance(dictionary, dict):
        dictionary = {}

    common_dict = dictionary.get("common")
    if not isinstance(common_dict, dict):
        common_dict = {}

    avia_dict = dictionary.get("avia")
    if not isinstance(avia_dict, dict):
        avia_dict = {}

    routes_dict = common_dict.get("routes")
    if not isinstance(routes_dict, dict):
        routes_dict = {}

    segments_dict = common_dict.get("segments")
    if not isinstance(segments_dict, dict):
        segments_dict = {}

    carriers_dict = common_dict.get("carriers")
    if not isinstance(carriers_dict, dict):
        carriers_dict = {}

    fare_apps_dict = common_dict.get("fareApplications")
    if not isinstance(fare_apps_dict, dict):
        fare_apps_dict = {}

    conditions_dict = avia_dict.get("conditions")
    if not isinstance(conditions_dict, dict):
        conditions_dict = {}

    vehicles_dict = avia_dict.get("vehicles")
    if not isinstance(vehicles_dict, dict):
        vehicles_dict = {}

    points_dict = avia_dict.get("points")
    if not isinstance(points_dict, dict):
        points_dict = {}

    cities_dict = common_dict.get("cities")
    if not isinstance(cities_dict, dict):
        cities_dict = {}

    is_roundtrip = query is not None and query.return_date is not None
    offers_by_key: dict[tuple[Any, ...], Offer] = {}
    seen_offer_ids: set[str] = set()

    for _offer_id, offer in actual_offers.items():
        if not isinstance(offer, dict):
            continue
        off_id = str(offer.get("id") or _offer_id)
        if off_id in seen_offer_ids:
            continue
        seen_offer_ids.add(off_id)
        try:
            route_ids = offer.get("routeIds")
            if not isinstance(route_ids, list) or not route_ids:
                continue

            if is_roundtrip:
                # В режиме туда-обратно пропускаем предложения без ровно двух непустых маршрутов
                if len(route_ids) != 2 or not route_ids[0] or not route_ids[1]:
                    continue
                out_info = _parse_route_leg(
                    route_ids[0],
                    routes_dict,
                    segments_dict,
                    carriers_dict,
                    vehicles_dict,
                    points_dict,
                    cities_dict,
                )
                back_info = _parse_route_leg(
                    route_ids[1],
                    routes_dict,
                    segments_dict,
                    carriers_dict,
                    vehicles_dict,
                    points_dict,
                    cities_dict,
                )
                if out_info is None or back_info is None:
                    continue

                back_leg: Leg | None = Leg(
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
                all_segment_ids: list[str] | None = (
                    out_info["segment_ids"] + back_info["segment_ids"]
                )
                dedup_key = (route_ids[0], route_ids[1])
            else:
                if not route_ids[0]:
                    continue
                out_info = _parse_route_leg(
                    route_ids[0],
                    routes_dict,
                    segments_dict,
                    carriers_dict,
                    vehicles_dict,
                    points_dict,
                    cities_dict,
                )
                if out_info is None:
                    continue
                back_leg = None
                all_segment_ids = None
                dedup_key = tuple(route_ids)

            variants = offer.get("offerVariants")
            if not isinstance(variants, list) or not variants:
                continue

            # Для каждого предложения берём САМЫЙ ДЕШЁВЫЙ из offerVariants
            best_var_price = float("inf")
            best_variant: dict[str, Any] | None = None
            currency = str((query.currency if query is not None else "RUB") or "RUB")

            for v in variants:
                if not isinstance(v, dict):
                    continue
                p_dict = v.get("price")
                if not isinstance(p_dict, dict):
                    continue
                val_dict = p_dict.get("value")
                if not isinstance(val_dict, dict):
                    continue
                raw_amount = val_dict.get("amount")
                raw_fraction = val_dict.get("fraction", 100)
                if raw_amount is None or isinstance(raw_amount, bool):
                    continue
                if raw_fraction is None or isinstance(raw_fraction, bool):
                    continue
                try:
                    amt = int(raw_amount)
                    frac = int(raw_fraction)
                    if frac <= 0 or amt <= 0:
                        continue
                    p = amt // frac
                    if p <= 0:
                        continue
                except (ValueError, TypeError, ZeroDivisionError):
                    continue

                curr = str(
                    val_dict.get("currencyCode")
                    or (query.currency if query is not None else "RUB")
                    or "RUB"
                )

                if p < best_var_price:
                    best_var_price = p
                    best_variant = v
                    currency = curr

            if best_variant is None or best_var_price == float("inf"):
                continue

            price = int(best_var_price)

            # Багаж
            baggage = (
                "с багажом"
                if _has_baggage(
                    best_variant,
                    fare_apps_dict,
                    conditions_dict,
                    required_segment_ids=all_segment_ids,
                )
                else "без багажа"
            )

            # Ручная кладь
            carry_on = _get_carry_on(
                best_variant,
                out_info["first_seg_id"],
                fare_apps_dict,
                conditions_dict,
            )

            # Ссылка на предложение
            pin_link = offer.get("pinLink")
            url = (
                pin_link
                if isinstance(pin_link, str) and pin_link.startswith("https://avia.tutu.ru")
                else None
            )

            offer_obj = Offer(
                site="tutu",
                price=price,
                currency=currency,
                airline=out_info["airline"],
                depart_time=out_info["depart_time"],
                arrive_time=out_info["arrive_time"],
                duration=out_info["duration"],
                transfers=out_info["transfers"],
                baggage=baggage,
                url=url,
                flight_numbers=out_info["flight_numbers"],
                aircraft=out_info["aircraft"],
                layovers=out_info["layovers"],
                depart_at=out_info["depart_at"],
                arrive_at=out_info["arrive_at"],
                carry_on=carry_on,
                back=back_leg,
            )

            # Дедупликация: сохраняем самый дешёвый вариант для данного ключа маршрута
            if dedup_key in offers_by_key:
                if price < offers_by_key[dedup_key].price:
                    offers_by_key[dedup_key] = offer_obj
            else:
                offers_by_key[dedup_key] = offer_obj
        except Exception:
            # Битые предложения пропускать, не падать
            continue

    offers = list(offers_by_key.values())
    offers.sort(key=lambda o: o.price)
    return offers


# Привязываем results_complete к функции парсера для pipeline.py
parse_tutu.results_complete = results_complete  # type: ignore[attr-defined]

