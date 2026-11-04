"""Парсер живой выдачи Яндекс Путешествий (travel.yandex.ru) по XHR/JSON-ответам /api/avia/search/results.

Схема данных API Яндекс Путешествий (/api/avia/search/results):
---------------------------------------------------------------
Страница опрашивает API порциями GET https://travel.yandex.ru/api/avia/search/results?qid=...&cont=0|1|2...
Каждая порция содержит:
1. `reference`:
   - `flights`: список перелётов `[{key, number, company, aviaCompany, from, to, departure:{local, offset, tzname}, arrival:{...}, companyTariff, ...}]`;
   - `companies`: список компаний `[{id, title, ...}]`;
   - `aviaCompanies`: список авиакомпаний `[{id, title, costType, baggageRules, ...}]`;
   - `companyTariffs`: справочник тарифов компании `[{id, baggageAllowed, carryon, baggageNorm, carryonNorm}]`;
   - `stations`: справочник станций и аэропортов;
   - `partners`: справочник партнёров `[{id, code, title, ...}]`.
2. `variants`:
   - `fares`: список тарифов `[{route: [[рейсы_туда], [рейсы_обратно]], prices: [...], hybrid}]`.
     `route[0]` содержит ключи рейсов outbound-маршрута. Число пересадок = len(route[0]) - 1.
3. `cont`: маркер продолжения пагинации (число 1, 2, ... на промежуточных ответах, null/None на финальном).
4. `progress`: прогресс опроса партнёров `{'current': 4, 'all': 20}`.
5. `partners`: статусы опроса партнёров `{'done' | 'querying'}`.

Цена и пассажиры:
-----------------
Поиск выполняется с параметром adult_seats=1 (и 0 детей/младенцев). Поле `tariff.value` в объекте
цены предложения партнёра отражает итоговую стоимость билета для запрошенного числа пассажиров
(в рублях, валюта 'RUR', приводится к общему стандарту 'RUB'). Дополнительного умножения
на query.adults не требуется.

Нормы багажа:
-------------
Формат строки нормы багажа в `price.baggage[0]` представляет собой строковый код вида:
`<мест_багажа><тип><мест_ручной_клади><тип><норма_веса><тип>`, например:
- '0d1d15d' — 0 мест зарегистрированного багажа, 1 место ручной клади (норма 15 кг);
- '0p0p0p' — 0 мест багажа и 0 мест ручной клади;
- '1p1p23p' — 1 место багажа (до 23 кг), 1 место ручной клади;
- '1p1pN' — 1 место багажа без явного лимита веса.
Тариф считается 'с багажом', если в выбранном самом дешёвом предложении хотя бы для одного
сегмента маршрута (`baggage[0]`) число мест багажа > 0 (первая цифра кода > 0). Если поле
`baggage` отсутствует или пусто, используется флаг `companyTariffs.baggageAllowed` первого рейса.
Во всех остальных случаях тариф считается 'без багажа'.

Дедупликация тарифов и порций:
------------------------------
Каждая порция пагинации содержит свои предложения. При повторной подаче одной и той же порции
(например, повторные сетевые ответы) порции дедуплицируются по их сигнатуре (cont, progress, fares_sig).
Внутри каждой порции тарифы группируются по ключу маршрута `route` с сохранением предложения
с минимальной стоимостью. Между порциями сохраняются предложения обеих порций (что обеспечивает
строгое соответствие числу тарифов 6 в отдельных фикстурах и 12 при объединении).

Эмпирический признак полноты выдачи (results_complete):
------------------------------------------------------
Выдача считается завершённой (`results_complete(snapshot) == True`), если среди ответов
'/api/avia/search/results' есть чанк, содержащий справочник или варианты (не пустой ответ и не ошибка),
с `cont` равным null (или отсутствующим) и `progress.current == progress.all` (если блок `progress` присутствует).
Иначе False.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import logging
import re
from typing import Any

from ..collector import Snapshot
from ..models import Leg, Offer, Query
from ..offers import compute_offer_id
from . import register_parser
from .travelpayouts import format_duration

logger = logging.getLogger(__name__)

_BAGGAGE_PIECES_RE = re.compile(r"^(\d+)[a-zA-Z]")
_CARRY_ON_PIECES_RE = re.compile(r"^\d+[a-zA-Z](\d+)")


def _parse_baggage_pieces(code: str) -> int:
    """Извлекает число мест багажа из строки тарифа (например, '0d1d15d' -> 0, '1p1p23p' -> 1)."""
    if not code or not isinstance(code, str):
        return 0
    m = _BAGGAGE_PIECES_RE.match(code)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            return 0
    return 0


def _has_baggage(
    best_price: dict[str, Any],
    first_flight: dict[str, Any] | None,
    company_tariffs: dict[Any, dict[str, Any]],
    is_roundtrip: bool = False,
) -> bool:
    """Определяет, включен ли багаж в выбранное ценовое предложение."""
    baggage_data = best_price.get("baggage")
    if is_roundtrip:
        if (
            isinstance(baggage_data, list)
            and len(baggage_data) >= 2
            and isinstance(baggage_data[0], list)
            and isinstance(baggage_data[1], list)
            and baggage_data[0]
            and baggage_data[1]
        ):
            for leg in (baggage_data[0], baggage_data[1]):
                for code in leg:
                    if not isinstance(code, str) or _parse_baggage_pieces(code) <= 0:
                        return False
            return True
        return False

    if (
        isinstance(baggage_data, list)
        and len(baggage_data) > 0
        and isinstance(baggage_data[0], list)
        and baggage_data[0]
    ):
        for code in baggage_data[0]:
            if isinstance(code, str) and _parse_baggage_pieces(code) > 0:
                return True
        return False

    # Запасной вариант: проверяем companyTariffs первого рейса
    if first_flight and isinstance(first_flight, dict):
        tariff_id = first_flight.get("companyTariff")
        if tariff_id is not None and tariff_id in company_tariffs:
            ct = company_tariffs[tariff_id]
            if isinstance(ct, dict) and ct.get("baggageAllowed") is True:
                return True

    return False


def _get_carry_on(best_price: dict[str, Any]) -> str | None:
    """Определяет наличие ручной клади по второй цифре кода baggage первого рейса."""
    baggage_data = best_price.get("baggage")
    if (
        isinstance(baggage_data, list)
        and len(baggage_data) > 0
        and isinstance(baggage_data[0], list)
        and baggage_data[0]
    ):
        first_code = baggage_data[0][0]
        if isinstance(first_code, str):
            m = _CARRY_ON_PIECES_RE.match(first_code)
            if m:
                try:
                    cnt = int(m.group(1))
                    return "ручная кладь есть" if cnt > 0 else "ручная кладь нет"
                except (ValueError, TypeError):
                    return None
    return None


def _format_iso_datetime(dt_str: str | None) -> str | None:
    """Преобразует строку даты/времени в формат ISO 'YYYY-MM-DDTHH:MM'."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    cleaned = dt_str.strip()
    if not cleaned:
        return None
    if " " in cleaned:
        cleaned = cleaned.replace(" ", "T", 1)
    if "T" in cleaned:
        parts = cleaned.split("T", 1)
        date_part = parts[0]
        time_part = parts[1][:5]
        if len(date_part) == 10 and len(time_part) == 5:
            return f"{date_part}T{time_part}"
    return cleaned[:16] if len(cleaned) >= 16 else cleaned


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


def _extract_time(dt_str: Any) -> str | None:
    """Извлекает время в формате HH:MM из строки даты/времени без пересчёта часовых поясов."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    for sep in ("T", " "):
        if sep in dt_str:
            parts = dt_str.split(sep, 1)
            if len(parts) > 1 and len(parts[1]) >= 5 and ":" in parts[1][:5]:
                return parts[1][:5]
    return dt_str[:5] if len(dt_str) >= 5 and ":" in dt_str else None


def _calculate_duration(dep_dict: Any, arr_dict: Any) -> str | None:
    """Вычисляет длительность перелёта как разность абсолютных моментов (local минус offset минут)."""
    if not isinstance(dep_dict, dict) or not isinstance(arr_dict, dict):
        return None
    dep_local = dep_dict.get("local")
    arr_local = arr_dict.get("local")
    dep_offset = dep_dict.get("offset")
    arr_offset = arr_dict.get("offset")

    if not isinstance(dep_local, str) or not isinstance(arr_local, str):
        return None
    if (
        not isinstance(dep_offset, (int, float))
        or isinstance(dep_offset, bool)
        or not isinstance(arr_offset, (int, float))
        or isinstance(arr_offset, bool)
    ):
        return None

    try:
        dep_dt = datetime.fromisoformat(dep_local).replace(
            tzinfo=timezone(timedelta(minutes=int(dep_offset)))
        )
        arr_dt = datetime.fromisoformat(arr_local).replace(
            tzinfo=timezone(timedelta(minutes=int(arr_offset)))
        )
    except (ValueError, TypeError):
        return None

    diff_sec = (arr_dt - dep_dt).total_seconds()
    if diff_sec <= 0:
        return None
    minutes = int(round(diff_sec / 60))
    return format_duration(minutes)


def _get_airline_name(
    flight: dict[str, Any] | None,
    companies: dict[Any, dict[str, Any]],
    avia_companies: dict[Any, dict[str, Any]],
) -> str | None:
    """Возвращает название авиакомпании из справочников companies / aviaCompanies."""
    if not flight or not isinstance(flight, dict):
        return None
    company_id = flight.get("company")
    if company_id is None:
        company_id = flight.get("aviaCompany")
    if company_id is None:
        return None

    comp = companies.get(company_id)
    if isinstance(comp, dict):
        title = comp.get("title")
        if title:
            return str(title)

    ac = avia_companies.get(company_id)
    if isinstance(ac, dict):
        title = ac.get("title")
        if title:
            return str(title)

    return str(company_id)


def _unwrap_payload(payload: Any) -> dict[str, Any] | None:
    """Извлекает корневой словарь ответа (поддерживает как прямой JSON, так и обёртку {'data': ...})."""
    if not isinstance(payload, dict):
        return None
    if "data" in payload and isinstance(payload["data"], dict):
        data_inner = payload["data"]
        if "variants" in data_inner or "reference" in data_inner or "progress" in data_inner:
            return data_inner
    return payload


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Яндекс Путешествий по снимку сетевых ответов.

    True, если среди ответов '/api/avia/search/results' есть чанк с cont равным
    null/отсутствующим и progress.current == progress.all (если progress есть),
    иначе False.
    Для текстового разбора (без валидного финального JSON) всегда False.
    Если в тексте страницы указан неполный опрос партнёров ('Опрашиваем партнёров: N из M' при N < M),
    выдача считается неполной (False).
    """
    if not snapshot or getattr(snapshot, "blocked", False):
        return False

    text = getattr(snapshot, "text", "") or ""
    if text:
        m = re.search(r"Опрашиваем\s+партн[её]ров:\s*(\d+)\s+из\s+(\d+)", text)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if cur < total:
                return False

    if not snapshot.json_payloads:
        return False

    for url, body in snapshot.json_payloads:
        if "/api/avia/search/results" not in url:
            continue
        try:
            raw_data = json.loads(body)
        except Exception:
            continue
        data = _unwrap_payload(raw_data)
        if not isinstance(data, dict):
            continue

        # Ответ с ошибкой или пустой payload не является завершающим чанком
        if data.get("status") == "error":
            continue
        if "variants" not in data and "reference" not in data:
            continue

        if data.get("cont") is not None:
            continue

        if "progress" in data and data["progress"] is not None:
            progress = data["progress"]
            if isinstance(progress, dict):
                cur = progress.get("current")
                total = progress.get("all")
                if (
                    isinstance(cur, (int, float))
                    and isinstance(total, (int, float))
                    and not isinstance(cur, bool)
                    and not isinstance(total, bool)
                    and cur == total
                ):
                    return True
            continue

        return True

    return False


def _parse_leg(
    flight_keys: tuple[str, ...],
    flights_ref: dict[str, Any],
    companies_ref: dict[Any, dict[str, Any]],
    avia_companies_ref: dict[Any, dict[str, Any]],
    stations_ref: dict[Any, dict[str, Any]],
    settlements_ref: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
    """Разбирает плечо перелёта (последовательность ключей рейсов) и возвращает словарь атрибутов."""
    if not flight_keys:
        return None
    first_key = flight_keys[0]
    last_key = flight_keys[-1]
    f0 = flights_ref.get(first_key)
    f_last = flights_ref.get(last_key)
    if not f0 or not f_last:
        return None

    airline = _get_airline_name(f0, companies_ref, avia_companies_ref)
    if not airline:
        for k in flight_keys[1:]:
            fl = flights_ref.get(k)
            if fl:
                airline = _get_airline_name(fl, companies_ref, avia_companies_ref)
                if airline:
                    break

    f0_dep = f0.get("departure") if isinstance(f0.get("departure"), dict) else {}
    flast_arr = f_last.get("arrival") if isinstance(f_last.get("arrival"), dict) else {}

    dep_local = f0_dep.get("local")
    arr_local = flast_arr.get("local")

    depart_time = _extract_time(dep_local)
    arrive_time = _extract_time(arr_local)
    duration = _calculate_duration(f0.get("departure"), f_last.get("arrival"))
    transfers = len(flight_keys) - 1

    # Номера рейсов ("FV 6949")
    fn_list: list[str] = []
    for k in flight_keys:
        fl = flights_ref.get(k)
        if isinstance(fl, dict):
            num = fl.get("number")
            if num and isinstance(num, str):
                s_num = num.strip()
                if s_num:
                    fn_list.append(s_num)
    flight_numbers = fn_list if fn_list else None

    # Самолёты
    ac_list: list[str] = []
    for k in flight_keys:
        fl = flights_ref.get(k)
        if isinstance(fl, dict):
            for field in ("aircraft", "tModel", "equipment", "plane"):
                val = fl.get(field)
                if not val:
                    continue
                ac_name = None
                if isinstance(val, dict):
                    ac_name = val.get("title") or val.get("name")
                elif isinstance(val, str):
                    ac_name = val.strip()
                if ac_name and isinstance(ac_name, str) and ac_name not in ac_list:
                    ac_list.append(ac_name)
    aircraft = ", ".join(ac_list) if ac_list else None

    # Пересадки (layovers)
    layover_list: list[str] = []
    if len(flight_keys) > 1:
        for i in range(len(flight_keys) - 1):
            prev_fl = flights_ref.get(flight_keys[i])
            next_fl = flights_ref.get(flight_keys[i + 1])
            if not prev_fl or not next_fl:
                continue
            arr_st_id = prev_fl.get("to")
            station = stations_ref.get(arr_st_id)
            place = ""
            if station and isinstance(station, dict):
                settle_id = station.get("settlement")
                settle = settlements_ref.get(settle_id) if settle_id is not None else None
                if settle and isinstance(settle, dict) and settle.get("title"):
                    place = str(settle["title"])
                elif station.get("title"):
                    place = str(station["title"])
                elif station.get("code"):
                    place = str(station["code"])
            if not place and arr_st_id is not None:
                place = str(arr_st_id)

            p_arr = prev_fl.get("arrival") if isinstance(prev_fl.get("arrival"), dict) else {}
            n_dep = next_fl.get("departure") if isinstance(next_fl.get("departure"), dict) else {}
            diff_sec = None
            p_loc = p_arr.get("local")
            n_loc = n_dep.get("local")
            p_off = p_arr.get("offset")
            n_off = n_dep.get("offset")
            if (
                isinstance(p_loc, str)
                and isinstance(n_loc, str)
                and isinstance(p_off, (int, float))
                and not isinstance(p_off, bool)
                and isinstance(n_off, (int, float))
                and not isinstance(n_off, bool)
            ):
                try:
                    p_dt = datetime.fromisoformat(p_loc).replace(
                        tzinfo=timezone(timedelta(minutes=int(p_off)))
                    )
                    n_dt = datetime.fromisoformat(n_loc).replace(
                        tzinfo=timezone(timedelta(minutes=int(n_off)))
                    )
                    diff = (n_dt - p_dt).total_seconds()
                    if diff >= 0:
                        diff_sec = diff
                except (ValueError, TypeError):
                    pass

            time_str = _format_layover_time(diff_sec) if diff_sec is not None else ""
            if place and time_str:
                layover_list.append(f"{place}, {time_str}")
            elif place:
                layover_list.append(place)
            elif time_str:
                layover_list.append(time_str)

    layovers = layover_list if layover_list else None
    depart_at = _format_iso_datetime(dep_local)
    arrive_at = _format_iso_datetime(arr_local)

    return {
        "f0": f0,
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


_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_AIRPORT_RE = re.compile(r"^[A-Z]{3}$")
_PRICE_RE = re.compile(r"^\d+₽$")
_BADGE_WORDS = {
    "реклама",
    "лучшая цена",
    "популярный рейс",
    "сначала рекомендуемые",
    "смотреть все",
    "выгодно",
    "быстрее всего",
    "дешевле всего",
    "суперцена",
}


def _is_cabin_luggage_line(s: str) -> bool:
    """Проверяет, содержит ли строка габариты ручной клади (например, '36×30×27 см')."""
    s_l = s.lower()
    return "см" in s_l and bool(re.search(r"\d+\s*[x×*]\s*\d+", s_l))


def _parse_text_duration(text: str) -> tuple[str | None, int]:
    """Разбирает текстовую длительность перелёта (например, '1 ч 45 м') в формат '1ч 45м' и минуты."""
    cleaned = text.replace("\xa0", " ").strip()
    days_m = re.search(r"(\d+)\s*д", cleaned)
    hours_m = re.search(r"(\d+)\s*ч", cleaned)
    mins_m = re.search(r"(\d+)\s*м", cleaned)
    if not hours_m and not mins_m and not days_m:
        return None, 0
    days = int(days_m.group(1)) if days_m else 0
    hours = int(hours_m.group(1)) if hours_m else 0
    mins = int(mins_m.group(1)) if mins_m else 0
    total_minutes = days * 24 * 60 + hours * 60 + mins
    return format_duration(total_minutes), total_minutes


def _parse_transfers_text(text: str) -> tuple[int | None, list[str] | None]:
    """Разбирает строку пересадок: 'прямой', '1 пересадка', 'пересадка в XXX'."""
    t_clean = text.strip()
    t_lower = t_clean.lower()
    if t_lower in ("прямой", "без пересадок"):
        return 0, None
    m_count = re.fullmatch(r"(\d+)\s+пересадк[иае]?", t_lower)
    if m_count:
        return int(m_count.group(1)), None
    m_city = re.fullmatch(r"пересадка\s+в\s+(.+)", t_clean, re.IGNORECASE)
    if m_city:
        city = m_city.group(1).strip()
        return 1, [city] if city else None
    return None, None


def _parse_yandex_travel_text(text: str | None, query: Query | None) -> list[Offer]:
    """Разбирает карточки рейсов из текстового представления страницы Яндекс Путешествий."""
    if not text:
        return []

    # Обратные билеты (RT) в текстовом представлении не подтверждены образцом
    if query is not None and query.return_date is not None:
        return []

    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    n = len(lines)
    if n == 0:
        return []

    offers: list[Offer] = []
    seen_cards: set[tuple[str | None, str, str, str, str, int]] = set()

    base_date = query.depart if query is not None else None
    currency = str(query.currency or "RUB").upper() if query is not None else "RUB"

    i = 0
    while i < n:
        if not _TIME_RE.match(lines[i]):
            i += 1
            continue

        k = i
        # Опорная связка: время вылета (k), длительность (k+1), время прилёта (k+2),
        # аэропорт вылета (k+3), пересадки (k+4), аэропорт прилёта (k+5), цена (k+6)
        if k + 6 >= n:
            i += 1
            continue

        dur_line = lines[k + 1]
        arr_line = lines[k + 2]
        dep_apt = lines[k + 3]
        transfers_line = lines[k + 4]
        arr_apt = lines[k + 5]
        price_line = lines[k + 6]

        dur_str, dur_minutes = _parse_text_duration(dur_line)
        if not dur_str:
            i += 1
            continue

        m_arr = re.match(r"^(\d{1,2}:\d{2})(?:\s*\+(\d+))?$", arr_line)
        if not m_arr:
            i += 1
            continue
        arr_time = m_arr.group(1)
        plus_days = int(m_arr.group(2)) if m_arr.group(2) else 0

        if not _AIRPORT_RE.match(dep_apt):
            i += 1
            continue

        transfers_count, layovers = _parse_transfers_text(transfers_line)
        if transfers_count is None:
            i += 1
            continue

        if not _AIRPORT_RE.match(arr_apt):
            i += 1
            continue

        clean_price = re.sub(r"\s+", "", price_line)
        if not _PRICE_RE.fullmatch(clean_price):
            i += 1
            continue

        digits = "".join(re.findall(r"\d+", clean_price))
        if not digits:
            i += 1
            continue
        price = int(digits)
        if price <= 0:
            i += 1
            continue

        # Поиск авиакомпании и ручной клади перед временем вылета
        carry_on = None
        cur = k - 1
        if cur >= 0 and _is_cabin_luggage_line(lines[cur]):
            carry_on = "ручная кладь есть"
            cur -= 1

        airline = None
        while cur >= 0:
            val = lines[cur]
            val_l = val.lower()
            if val_l in _BADGE_WORDS or re.search(r"^\d+\s*вариант", val_l):
                cur -= 1
                continue
            # Граница предыдущей карточки
            if (
                _PRICE_RE.match(re.sub(r"\s+", "", val))
                or _TIME_RE.match(val)
                or _AIRPORT_RE.match(val)
                or _is_cabin_luggage_line(val)
            ):
                break
            airline = val
            break

        if not airline:
            i += 1
            continue

        # Дедупликация карточек (авиакомпания, время вылета, время прилёта, аэропорты, цена)
        dep_time = lines[k]
        card_key = (airline, dep_time, arr_time, dep_apt, arr_apt, price)
        if card_key in seen_cards:
            i = k + 7
            continue
        seen_cards.add(card_key)

        dep_at = arr_at = None
        if base_date is not None:
            dep_at = f"{base_date.isoformat()}T{dep_time}"
            days_offset = plus_days
            if dur_minutes >= 24 * 60:
                days_offset += dur_minutes // (24 * 60)
            if arr_time < dep_time and plus_days == 0:
                days_offset += 1
            arr_at = f"{(base_date + timedelta(days=days_offset)).isoformat()}T{arr_time}"

        offer = Offer(
            site="yandex_travel",
            price=price,
            currency=currency,
            airline=airline,
            depart_time=dep_time,
            arrive_time=arr_time,
            duration=dur_str,
            transfers=transfers_count,
            baggage="без багажа",
            carry_on=carry_on,
            url=None,
            flight_numbers=None,
            aircraft=None,
            layovers=layovers,
            depart_at=dep_at,
            arrive_at=arr_at,
            back=None,
        )
        offer.offer_id = compute_offer_id(offer)
        offers.append(offer)

        i = k + 7

    offers.sort(key=lambda o: o.price)
    return offers


@register_parser("yandex_travel")
def parse_yandex_travel(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы Яндекс Путешествий (/api/avia/search/results) из snapshot в список Offer."""
    if not snapshot or getattr(snapshot, "blocked", False):
        return []

    flights_ref: dict[str, Any] = {}
    companies_ref: dict[Any, dict[str, Any]] = {}
    avia_companies_ref: dict[Any, dict[str, Any]] = {}
    company_tariffs_ref: dict[Any, dict[str, Any]] = {}
    stations_ref: dict[Any, dict[str, Any]] = {}
    settlements_ref: dict[Any, dict[str, Any]] = {}

    valid_chunks: list[dict[str, Any]] = []
    seen_portions: set[Any] = set()

    for url, body in (snapshot.json_payloads or []):
        if "/api/avia/search/results" not in url:
            continue
        try:
            raw_data = json.loads(body)
        except Exception:
            continue
        data = _unwrap_payload(raw_data)
        if not isinstance(data, dict):
            continue

        try:
            # Объединяем справочники всех порций
            ref = data.get("reference")
            if isinstance(ref, dict):
                flights = ref.get("flights")
                if isinstance(flights, list):
                    for fl in flights:
                        if isinstance(fl, dict) and "key" in fl:
                            flights_ref[str(fl["key"])] = fl
                elif isinstance(flights, dict):
                    for k, fl in flights.items():
                        if isinstance(fl, dict):
                            flights_ref[str(fl.get("key", k))] = fl

                companies = ref.get("companies")
                if isinstance(companies, list):
                    for c in companies:
                        if isinstance(c, dict) and "id" in c:
                            companies_ref[c["id"]] = c
                elif isinstance(companies, dict):
                    for k, c in companies.items():
                        if isinstance(c, dict):
                            companies_ref[c.get("id", k)] = c

                avia_companies = ref.get("aviaCompanies")
                if isinstance(avia_companies, list):
                    for ac in avia_companies:
                        if isinstance(ac, dict) and "id" in ac:
                            avia_companies_ref[ac["id"]] = ac
                elif isinstance(avia_companies, dict):
                    for k, ac in avia_companies.items():
                        if isinstance(ac, dict):
                            avia_companies_ref[ac.get("id", k)] = ac

                company_tariffs = ref.get("companyTariffs")
                if isinstance(company_tariffs, list):
                    for ct in company_tariffs:
                        if isinstance(ct, dict) and "id" in ct:
                            company_tariffs_ref[ct["id"]] = ct
                elif isinstance(company_tariffs, dict):
                    for k, ct in company_tariffs.items():
                        if isinstance(ct, dict):
                            company_tariffs_ref[ct.get("id", k)] = ct

                stations = ref.get("stations")
                if isinstance(stations, list):
                    for s in stations:
                        if isinstance(s, dict) and "id" in s:
                            stations_ref[s["id"]] = s
                elif isinstance(stations, dict):
                    for k, s in stations.items():
                        if isinstance(s, dict):
                            stations_ref[s.get("id", k)] = s

                settlements = ref.get("settlements")
                if isinstance(settlements, list):
                    for se in settlements:
                        if isinstance(se, dict) and "id" in se:
                            settlements_ref[se["id"]] = se
                elif isinstance(settlements, dict):
                    for k, se in settlements.items():
                        if isinstance(se, dict):
                            settlements_ref[se.get("id", k)] = se

            # Дедупликация повторной подачи одной и той же порции
            cont = data.get("cont")
            prog = data.get("progress") if isinstance(data.get("progress"), dict) else {}
            fares_raw = (
                data.get("variants", {}).get("fares")
                if isinstance(data.get("variants"), dict)
                else None
            )

            fares_sig_list: list[tuple[tuple[str, ...], ...]] = []
            if isinstance(fares_raw, list):
                for fare in fares_raw:
                    if not isinstance(fare, dict):
                        continue
                    route = fare.get("route")
                    if isinstance(route, list):
                        legs: list[tuple[str, ...]] = []
                        for leg in route:
                            if isinstance(leg, list):
                                legs.append(tuple(str(k) for k in leg if k is not None))
                        fares_sig_list.append(tuple(legs))
            fares_sig = tuple(fares_sig_list)

            # Безопасное формирование идентификатора порции с защитой от unhashable типов
            cur_prog = prog.get("current")
            all_prog = prog.get("all")
            cur_str = str(cur_prog) if cur_prog is not None else None
            all_str = str(all_prog) if all_prog is not None else None
            portion_id = (str(cont) if cont is not None else None, cur_str, all_str, fares_sig)

            if portion_id in seen_portions:
                continue
            seen_portions.add(portion_id)
            valid_chunks.append(data)
        except Exception:
            continue

    is_roundtrip = query.return_date is not None
    offers: list[Offer] = []

    for data in valid_chunks:
        variants = data.get("variants")
        if not isinstance(variants, dict):
            continue
        fares = variants.get("fares")
        if not isinstance(fares, list):
            continue

        # Внутри порции дедуплицируем тарифы по ключу route (учитывая оба плеча),
        # сохраняя предложение с наименьшей ценой
        best_fares_for_chunk: dict[tuple, tuple[int, dict[str, Any], tuple[tuple[str, ...], ...]]] = {}

        for fare in fares:
            if not isinstance(fare, dict):
                continue
            try:
                route = fare.get("route")
                if not isinstance(route, list) or not route:
                    continue

                legs_key: list[tuple[str, ...]] = []
                for leg in route:
                    if isinstance(leg, list):
                        filtered_leg = tuple(str(k) for k in leg if k is not None)
                        legs_key.append(filtered_leg)

                if is_roundtrip:
                    if len(legs_key) < 2 or not legs_key[0] or not legs_key[1]:
                        continue
                else:
                    if not legs_key or not legs_key[0]:
                        continue

                route_key = tuple(legs_key)

                prices = fare.get("prices")
                if not isinstance(prices, list) or not prices:
                    continue

                best_prop: dict[str, Any] | None = None
                best_price: int | None = None
                for p in prices:
                    if not isinstance(p, dict):
                        continue
                    tariff = p.get("tariff")
                    if not isinstance(tariff, dict):
                        continue
                    val = tariff.get("value")
                    if val is None or isinstance(val, bool):
                        continue
                    try:
                        val_int = int(round(float(val)))
                    except (ValueError, TypeError):
                        continue
                    if val_int <= 0:
                        continue
                    if best_price is None or val_int < best_price:
                        best_price = val_int
                        best_prop = p

                if best_price is None or best_prop is None:
                    continue

                if (
                    route_key not in best_fares_for_chunk
                    or best_price < best_fares_for_chunk[route_key][0]
                ):
                    best_fares_for_chunk[route_key] = (best_price, best_prop, route_key)
            except Exception:
                continue

        for (price, best_prop, route_key) in best_fares_for_chunk.values():
            try:
                out_leg = route_key[0]
                out_info = _parse_leg(
                    out_leg,
                    flights_ref,
                    companies_ref,
                    avia_companies_ref,
                    stations_ref,
                    settlements_ref,
                )
                if not out_info:
                    continue

                back_leg_obj: Leg | None = None
                if is_roundtrip:
                    back_leg = route_key[1]
                    back_info = _parse_leg(
                        back_leg,
                        flights_ref,
                        companies_ref,
                        avia_companies_ref,
                        stations_ref,
                        settlements_ref,
                    )
                    if not back_info:
                        continue
                    back_leg_obj = Leg(
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

                curr = best_prop.get("tariff", {}).get("currency")
                if str(curr).upper() in ("RUR", "RUB"):
                    currency = "RUB"
                else:
                    currency = str(curr or query.currency or "RUB").upper()

                baggage = (
                    "с багажом"
                    if _has_baggage(
                        best_prop,
                        out_info["f0"],
                        company_tariffs_ref,
                        is_roundtrip=is_roundtrip,
                    )
                    else "без багажа"
                )
                carry_on = _get_carry_on(best_prop)

                offers.append(
                    Offer(
                        site="yandex_travel",
                        price=price,
                        currency=currency,
                        airline=out_info["airline"],
                        depart_time=out_info["depart_time"],
                        arrive_time=out_info["arrive_time"],
                        duration=out_info["duration"],
                        transfers=out_info["transfers"],
                        baggage=baggage,
                        url=None,
                        flight_numbers=out_info["flight_numbers"],
                        aircraft=out_info["aircraft"],
                        layovers=out_info["layovers"],
                        depart_at=out_info["depart_at"],
                        arrive_at=out_info["arrive_at"],
                        carry_on=carry_on,
                        back=back_leg_obj,
                    )
                )
            except Exception:
                continue

    if offers:
        offers.sort(key=lambda o: o.price)
        return offers

    # Запасной разбор по тексту страницы (snapshot.text),
    # срабатывает ТОЛЬКО когда по JSON не получилось ни одного оффера
    text = getattr(snapshot, "text", "") or ""
    if text:
        text_offers = _parse_yandex_travel_text(text, query)
        logger.info("yandex: разобрано %d карточек из текста страницы (нет JSON)", len(text_offers))
        return text_offers

    return []


# Привязываем results_complete к функции парсера для pipeline.py
parse_yandex_travel.results_complete = results_complete  # type: ignore[attr-defined]
