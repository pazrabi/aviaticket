from __future__ import annotations

import dataclasses
import hashlib
import re
import time
from typing import Sequence

from .models import Leg, Offer, OfferAlt, SiteResult


def _leg_parts(leg: Offer | Leg) -> list[str]:
    fn = ",".join(leg.flight_numbers) if leg.flight_numbers else ""
    dep = leg.depart_at or leg.depart_time or ""
    arr = leg.arrive_at or leg.arrive_time or ""
    dur = leg.duration or ""
    transfers = "" if leg.transfers is None else str(leg.transfers)
    airline = leg.airline or ""
    return [airline, fn, dep, arr, dur, transfers]


def compute_offer_id(o: Offer) -> str:
    """Вычисляет стабильный 12-значный sha1-хэш идентификатора билета без цены и ссылки."""
    parts = [o.site, *_leg_parts(o)]
    if o.back is not None:
        parts.extend(_leg_parts(o.back))
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def baggage_state(o: Offer) -> bool | None:
    """Определяет наличие багажа: True (с багажом), False (без багажа), None (не указано)."""
    if not o.baggage:
        return None
    val = o.baggage.lower()
    if "без багажа" in val:
        return False
    if "с багажом" in val:
        return True
    return None


def filter_by_baggage(results: Sequence[SiteResult], mode: str) -> tuple[list[SiteResult], int]:
    """Фильтрует результаты по наличию багажа, не мутируя исходные объекты."""
    if mode not in {"any", "with", "without"}:
        raise ValueError(f"Неизвестный режим фильтрации багажа: {mode}")

    new_results: list[SiteResult] = []
    dropped_count = 0

    for r in results:
        if mode == "any":
            kept_offers = list(r.offers)
        elif mode == "with":
            kept_offers = [o for o in r.offers if baggage_state(o) is not False]
            dropped_count += len(r.offers) - len(kept_offers)
        elif mode == "without":
            kept_offers = [o for o in r.offers if baggage_state(o) is not True]
            dropped_count += len(r.offers) - len(kept_offers)

        new_results.append(dataclasses.replace(r, offers=kept_offers))

    return new_results, dropped_count


def assign_ids(results: Sequence[SiteResult], now: float | None = None) -> Sequence[SiteResult]:
    """Проставляет offer_id и fetched_at там, где они не заполнены."""
    ts = time.time() if now is None else now
    for r in results:
        for o in r.offers:
            if not o.offer_id:
                o.offer_id = compute_offer_id(o)
            if o.fetched_at is None:
                o.fetched_at = ts
    return results


def normalize_airline(airline: str | None) -> str:
    """Нормализует название авиакомпании: нижний регистр, первое слово, без знаков."""
    if not airline:
        return ""
    clean = re.sub(r"[^\w\s]|_", " ", airline.lower())
    words = clean.split()
    return words[0] if words else ""


def _normalize_time_str(val: str | None) -> str | None:
    if not val:
        return None
    s = val.strip()
    if not s:
        return None
    if len(s) >= 16 and (s[10] == "T" or s[10] == " "):
        return f"{s[:10]}T{s[11:16]}"
    if len(s) == 8 and s.count(":") == 2:
        return s[:5]
    return s


def flight_key(o: Offer) -> tuple | None:
    """Ключ рейса без сайта и цены: время вылета/прилёта туда, то же для o.back, пересадки, валюта."""
    dep = _normalize_time_str(o.depart_at or o.depart_time)
    if not dep:
        return None
    arr = _normalize_time_str(o.arrive_at or o.arrive_time)

    if o.back is not None:
        back_dep = _normalize_time_str(o.back.depart_at or o.back.depart_time)
        if not back_dep:
            return None
        back_arr = _normalize_time_str(o.back.arrive_at or o.back.arrive_time)
        back_transfers = o.back.transfers
    else:
        back_dep = None
        back_arr = None
        back_transfers = None

    return (dep, arr, back_dep, back_arr, o.transfers, back_transfers, o.currency)


def same_flight(a: Offer, b: Offer) -> bool:
    """Проверяет, относятся ли два билета к одному рейсу (время равно, авиакомпании совместимы)."""
    ka = flight_key(a)
    kb = flight_key(b)
    if ka is None or kb is None or ka != kb:
        return False
    air_a = normalize_airline(a.airline)
    air_b = normalize_airline(b.airline)
    if air_a and air_b and air_a != air_b:
        return False
    if a.back is not None and b.back is not None:
        air_ba = normalize_airline(a.back.airline)
        air_bb = normalize_airline(b.back.airline)
        if air_ba and air_bb and air_ba != air_bb:
            return False
    return True


def merge_same_flights(offers: Sequence[Offer]) -> list[Offer]:
    """Склеивает билеты разных сайтов с одним рейсом в один билет (представитель самый дешёвый)."""
    if not offers:
        return []

    orig_idx = {id(o): i for i, o in enumerate(offers)}

    # 1. Группировка по ключу времени (flight_key). Билеты без времени вылета не склеиваются.
    groups_by_key: dict[tuple, list[Offer]] = {}
    unmergeable: list[Offer] = []

    for o in offers:
        k = flight_key(o)
        if k is None:
            unmergeable.append(o)
        else:
            groups_by_key.setdefault(k, []).append(o)

    merged_entries: list[tuple[Offer, int]] = []

    for o in unmergeable:
        merged_entries.append((o, orig_idx[id(o)]))

    # 2. Внутри каждой группы по времени разбиваем по совместимости авиакомпаний
    for key, time_group in groups_by_key.items():
        sorted_by_airline = sorted(
            time_group,
            key=lambda o: (0 if normalize_airline(o.airline) else 1, orig_idx[id(o)]),
        )
        subgroups: list[list[Offer]] = []
        for o in sorted_by_airline:
            target = None
            for sg in subgroups:
                if all(same_flight(o, member) for member in sg):
                    target = sg
                    break
            if target is not None:
                target.append(o)
            else:
                subgroups.append([o])

        # 3. Внутри каждой подгруппы формируем билеты (один рейс с разных сайтов)
        for cluster in subgroups:
            remaining = sorted(cluster, key=lambda o: (o.price, orig_idx[id(o)]))
            while remaining:
                rep = remaining.pop(0)
                used_sites = {rep.site}
                alt_offers: list[Offer] = []
                new_remaining: list[Offer] = []
                for cand in remaining:
                    if cand.site not in used_sites:
                        used_sites.add(cand.site)
                        alt_offers.append(cand)
                    else:
                        new_remaining.append(cand)
                remaining = new_remaining

                if alt_offers:
                    alt_offers.sort(key=lambda a: (a.price, orig_idx[id(a)]))
                    alts = [
                        OfferAlt(
                            site=ao.site,
                            price=ao.price,
                            currency=ao.currency,
                            url=ao.url,
                            url_is_direct=ao.url_is_direct,
                        )
                        for ao in alt_offers
                    ]
                    merged = dataclasses.replace(rep, alts=alts)
                else:
                    merged = rep

                merged_entries.append((merged, orig_idx[id(rep)]))

    # 4. Стабильный порядок результата: по цене представителя, затем по исходному порядку
    merged_entries.sort(key=lambda item: (item[0].price, item[1]))
    return [item[0] for item in merged_entries]
