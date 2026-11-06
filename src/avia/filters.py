from __future__ import annotations

from collections.abc import Sequence
import dataclasses
from datetime import datetime
import re

from .criteria import SearchCriteria
from .models import Offer, SiteResult
from .offers import baggage_state

# Пороги для карточки «Золотая середина» и отбора «5 билетов на сайт»
BALANCE_MAX_PREMIUM_PCT = 30.0
BALANCE_MIN_SAVING_MINUTES = 120
BALANCE_MIN_SAVING_SHARE = 0.25
SITE_TOP_CHEAP = 2
SITE_TOP_BALANCE = 2
SITE_TOP_FAST = 1

# Для обратной совместимости
MIDDLE_MAX_EXTRA_RATIO = BALANCE_MAX_PREMIUM_PCT / 100.0
MIDDLE_MIN_TIME_GAIN_RATIO = BALANCE_MIN_SAVING_SHARE
MIDDLE_MIN_TIME_GAIN_MINUTES = BALANCE_MIN_SAVING_MINUTES
MIDDLE_DEFAULT_HOUR_COST = 300

_DURATION_REGEX = re.compile(
    r"^(?:(?P<days>\d+)\s*(?:д(?:ней|ня|ень|н)?|d(?:ays?)?)\.?)?\s*"
    r"(?:(?P<hours>\d+)\s*(?:ч(?:асов|аса|ас)?|h(?:ours?|r)?)\.?)?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:м(?:инут[ыа]?|ин)?|m(?:in(?:utes?)?)?)\.?)?$",
    re.IGNORECASE,
)


def parse_duration_minutes(text: str | None) -> int | None:
    if not isinstance(text, str):
        return None
    cleaned = text.replace("\u00a0", " ").strip()
    if not cleaned:
        return None
    m = _DURATION_REGEX.match(cleaned)
    if not m:
        return None
    days_s = m.group("days")
    hours_s = m.group("hours")
    minutes_s = m.group("minutes")
    if days_s is None and hours_s is None and minutes_s is None:
        return None
    days = int(days_s) if days_s else 0
    hours = int(hours_s) if hours_s else 0
    minutes = int(minutes_s) if minutes_s else 0
    total = days * 1440 + hours * 60 + minutes
    return total if total >= 0 else None


def _extract_time(time_str: str | None, iso_str: str | None) -> tuple[int, int] | None:
    if isinstance(time_str, str):
        s = time_str.strip()
        parts = s.split(":")
        if len(parts) >= 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return h, m
            except (ValueError, TypeError):
                pass
    if isinstance(iso_str, str):
        s = iso_str.strip()
        if "T" not in s and " " not in s and ":" not in s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.hour, dt.minute
        except (ValueError, TypeError):
            m = re.search(r"(?:^|[T\s])(\d{1,2}):(\d{2})", s)
            if m:
                try:
                    h, minute = int(m.group(1)), int(m.group(2))
                    if 0 <= h <= 23 and 0 <= minute <= 59:
                        return h, minute
                except (ValueError, TypeError):
                    pass
    return None


def _get_part_of_day(hour: int, minute: int) -> str | None:
    if 0 <= hour <= 5:
        return "night"
    elif 6 <= hour <= 11:
        return "morning"
    elif 12 <= hour <= 17:
        return "day"
    elif 18 <= hour <= 23:
        return "evening"
    return None


def total_duration_minutes(offer: Offer) -> int | None:
    dur1 = parse_duration_minutes(offer.duration)
    if offer.back is None:
        return dur1
    dur2 = parse_duration_minutes(offer.back.duration)
    if dur1 is not None and dur2 is not None:
        return dur1 + dur2
    return None


def total_transfers(offer: Offer) -> int | None:
    """Суммарное число пересадок по обоим плечам или None, если данные о пересадках отсутствуют."""
    if offer.transfers is None:
        return None
    tot = offer.transfers
    if offer.back is not None:
        if offer.back.transfers is None:
            return None
        tot += offer.back.transfers
    return tot


def is_noticeably_better(
    candidate: Offer,
    cheapest: Offer,
    settings: Any = None,
    *,
    baggage_mode: str = "any",
    min_saving_minutes: int | None = None,
    min_saving_share: float | None = None,
) -> bool:
    """Проверяет, что кандидат заметно лучше самого дешёвого билета:
    хотя бы одно из:
    - суммарное время короче хотя бы на BALANCE_MIN_SAVING_MINUTES (120 мин)
      И хотя бы на BALANCE_MIN_SAVING_SHARE (0.25) от времени дешёвого;
    - есть багаж (baggage_state is True), а у дешёвого нет (baggage_state is not True).
      Условие по багажу действует только в режиме baggage_mode == 'any'.
    Условие «меньше пересадок» убрано по правилу владельца.
    """
    if isinstance(settings, str):
        baggage_mode = settings
        settings = None

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

    # 1. «заметно быстрее»
    dur_cheap = total_duration_minutes(cheapest)
    dur_cand = total_duration_minutes(candidate)
    if dur_cheap is not None and dur_cand is not None:
        time_gain = dur_cheap - dur_cand
        if (
            time_gain >= saving_min
            and time_gain >= dur_cheap * saving_share - 1e-9
        ):
            return True

    # 2. «с багажом» (только в режиме 'any')
    if baggage_mode == "any":
        if baggage_state(candidate) is True and baggage_state(cheapest) is not True:
            return True

    return False


_is_noticeably_better = is_noticeably_better


def matches(offer: Offer, criteria: SearchCriteria) -> bool:
    if criteria.is_default():
        return True

    # 1. max_transfers (applied to both legs if back is present)
    if criteria.max_transfers is not None:
        if offer.transfers is not None and offer.transfers > criteria.max_transfers:
            return False
        if offer.back is not None:
            if offer.back.transfers is not None and offer.back.transfers > criteria.max_transfers:
                return False

    # 2. max_duration_hours (each leg checked separately)
    if criteria.max_duration_hours is not None:
        max_mins = criteria.max_duration_hours * 60
        out_dur = parse_duration_minutes(offer.duration)
        if out_dur is not None and out_dur > max_mins:
            return False
        if offer.back is not None:
            back_dur = parse_duration_minutes(offer.back.duration)
            if back_dur is not None and back_dur > max_mins:
                return False

    # 3. depart_parts (only outbound leg)
    if criteria.depart_parts:
        t = _extract_time(offer.depart_time, offer.depart_at)
        if t is not None:
            part = _get_part_of_day(t[0], t[1])
            if part is not None and part not in criteria.depart_parts:
                return False

    # 4. arrive_parts (only outbound leg)
    if criteria.arrive_parts:
        t = _extract_time(offer.arrive_time, offer.arrive_at)
        if t is not None:
            part = _get_part_of_day(t[0], t[1])
            if part is not None and part not in criteria.arrive_parts:
                return False

    return True


def apply_criteria(
    results: Sequence[SiteResult], criteria: SearchCriteria
) -> tuple[list[SiteResult], int]:
    if criteria.is_default():
        return list(results), 0

    new_results: list[SiteResult] = []
    dropped_count = 0

    for r in results:
        kept_offers = [o for o in r.offers if matches(o, criteria)]
        dropped_count += len(r.offers) - len(kept_offers)
        new_results.append(dataclasses.replace(r, offers=kept_offers))

    return new_results, dropped_count


def pareto_front(offers: Sequence[Offer]) -> list[Offer]:
    durations = {id(o): total_duration_minutes(o) for o in offers}
    kept: list[Offer] = []

    for o in offers:
        dur_o = durations[id(o)]
        if dur_o is None:
            # Билеты без известного времени не отбрасывать и не использовать для доминирования
            kept.append(o)
            continue

        dominated = False
        for other in offers:
            if other is o:
                continue
            dur_other = durations[id(other)]
            if dur_other is None:
                continue

            if (other.price <= o.price and dur_other <= dur_o) and (
                other.price < o.price or dur_other < dur_o
            ):
                dominated = True
                break

        if not dominated:
            kept.append(o)

    kept.sort(key=lambda o: o.price)
    return kept


def balance_score(offer: Offer, hour_cost: int | float) -> float:
    tot_dur = total_duration_minutes(offer)
    dur_cost = (tot_dur / 60.0 * hour_cost) if tot_dur is not None else 0.0

    transfers = 0
    if offer.transfers is not None:
        transfers += offer.transfers
    if offer.back is not None and offer.back.transfers is not None:
        transfers += offer.back.transfers
    transfers_cost = 500.0 * transfers

    return float(offer.price + dur_cost + transfers_cost)


def _is_same_offer(a: Offer, b: Offer) -> bool:
    if a is b:
        return True
    if a.offer_id and b.offer_id and a.offer_id == b.offer_id:
        return True
    return a == b


def pick_cards(
    offers: Sequence[Offer],
    criteria: SearchCriteria,
    settings: Any = None,
    *,
    baggage_mode: str = "any",
    max_premium_pct: float | None = None,
    min_saving_minutes: int | None = None,
    min_saving_share: float | None = None,
) -> list[tuple[str, Offer]]:
    if not offers:
        return []

    cards: list[tuple[str, Offer]] = []
    chosen: list[Offer] = []

    # 1. («Самый дешёвый», min цена)
    cheapest = min(offers, key=lambda o: o.price)
    cards.append(("Самый дешёвый", cheapest))
    chosen.append(cheapest)

    # 2. («Самый быстрый», min время, только если это другой билет и время известно)
    offers_with_time = [o for o in offers if total_duration_minutes(o) is not None]
    if offers_with_time:
        fastest = min(offers_with_time, key=lambda o: (total_duration_minutes(o), o.price))
        if not any(_is_same_offer(fastest, x) for x in chosen):
            cards.append(("Самый быстрый", fastest))
            chosen.append(fastest)

    # Определяем билет для «Лучший баланс», если criteria.hour_cost задан
    best_balance: Offer | None = None
    if criteria.hour_cost is not None:
        cand_balance = min(
            offers, key=lambda o: (balance_score(o, criteria.hour_cost), o.price)
        )
        if not any(_is_same_offer(cand_balance, x) for x in chosen):
            best_balance = cand_balance

    # 3. («Золотая середина», до BALANCE_MAX_PREMIUM_PCT% дороже дешёвого и заметно лучше по удобству)
    prem_pct = (
        max_premium_pct
        if max_premium_pct is not None
        else (
            settings.balance_max_premium_pct
            if settings and hasattr(settings, "balance_max_premium_pct")
            else BALANCE_MAX_PREMIUM_PCT
        )
    )
    max_price = cheapest.price * (1.0 + prem_pct / 100.0)
    middle_candidates: list[Offer] = []
    for o in offers:
        if any(_is_same_offer(o, x) for x in chosen):
            continue
        if o.price >= max_price:
            continue
        if not is_noticeably_better(
            o,
            cheapest,
            settings,
            baggage_mode=baggage_mode,
            min_saving_minutes=min_saving_minutes,
            min_saving_share=min_saving_share,
        ):
            continue
        middle_candidates.append(o)

    if middle_candidates:
        best_middle = min(middle_candidates, key=lambda o: o.price)
        # Если ровно тот же билет уже выбран как «Лучший баланс», «Золотая середина» не добавляется (не дублировать).
        if best_balance is None or not _is_same_offer(best_middle, best_balance):
            cards.append(("Золотая середина", best_middle))
            chosen.append(best_middle)

    # 4. («Лучший баланс», min balance_score, только если criteria.hour_cost задан и билет отличается от уже выбранных)
    if best_balance is not None and not any(_is_same_offer(best_balance, x) for x in chosen):
        cards.append(("Лучший баланс", best_balance))
        chosen.append(best_balance)

    return cards


def pick_site_top(
    offers: Sequence[Offer],
    settings: Any = None,
    baggage_mode: str = "any",
) -> list[Offer]:
    """Из офферов ОДНОГО сайта (после фильтров багажа и критериев, отсортированных по цене)
    выбрать до 5 билетов:
    - SITE_TOP_CHEAP (по умолчанию 2) самых дешёвых по цене;
    - SITE_TOP_BALANCE (по умолчанию 2) «золотой середины» по правилу выше среди оставшихся
      (ориентир — самый дешёвый билет этого сайта);
    - SITE_TOP_FAST (по умолчанию 1) самый быстрый по total_duration_minutes среди оставшихся
      (билеты без известного времени не участвуют).
    Один билет занимает одно место (_is_same_offer).
    Если для какого-то слота нет кандидата (нет времени в пути, никто не прошёл правило середины,
    у сайта меньше 5 билетов), место добирается следующим по цене.
    Результат отсортирован по цене.
    """
    if isinstance(settings, str):
        baggage_mode = settings
        settings = None

    if not offers:
        return []

    # Очищаем старые метки ролей на случай переиспользования объектов
    for o in offers:
        if hasattr(o, "_site_top_role"):
            delattr(o, "_site_top_role")

    n_cheap = (
        settings.site_top_cheap
        if settings and hasattr(settings, "site_top_cheap")
        else SITE_TOP_CHEAP
    )
    n_balance = (
        settings.site_top_balance
        if settings and hasattr(settings, "site_top_balance")
        else SITE_TOP_BALANCE
    )
    n_fast = (
        settings.site_top_fast
        if settings and hasattr(settings, "site_top_fast")
        else SITE_TOP_FAST
    )
    max_premium_pct = (
        settings.balance_max_premium_pct
        if settings and hasattr(settings, "balance_max_premium_pct")
        else BALANCE_MAX_PREMIUM_PCT
    )

    # Список офферов отсортирован по цене
    sorted_offers = sorted(offers, key=lambda o: o.price)
    cheapest = sorted_offers[0]

    chosen: list[Offer] = []
    balance_offers: list[Offer] = []
    fast_offers: list[Offer] = []

    def is_chosen(offer: Offer) -> bool:
        return any(_is_same_offer(offer, c) for c in chosen)

    # 1. SITE_TOP_CHEAP самых дешёвых по цене
    for o in sorted_offers:
        if not is_chosen(o):
            chosen.append(o)
            o._site_top_role = "cheap"
            if len(chosen) >= n_cheap:
                break

    # 2. SITE_TOP_BALANCE «золотой середины» среди оставшихся (ориентир — самый дешёвый билет сайта)
    max_price = cheapest.price * (1.0 + max_premium_pct / 100.0)
    for o in sorted_offers:
        if len(balance_offers) >= n_balance:
            break
        if is_chosen(o):
            continue
        if o.price >= max_price:
            continue
        if is_noticeably_better(o, cheapest, settings=settings, baggage_mode=baggage_mode):
            chosen.append(o)
            balance_offers.append(o)
            o._site_top_role = "balance"

    # 3. SITE_TOP_FAST самый быстрый по total_duration_minutes среди оставшихся
    candidates_with_time = [
        o for o in sorted_offers if not is_chosen(o) and total_duration_minutes(o) is not None
    ]
    if candidates_with_time and n_fast > 0:
        fastest_rem = min(
            candidates_with_time, key=lambda o: (total_duration_minutes(o), o.price)
        )
        chosen.append(fastest_rem)
        fast_offers.append(fastest_rem)
        fastest_rem._site_top_role = "fast"

    # 4. Если слоты не заполнены, место добирается следующим по цене
    target_count = n_cheap + n_balance + n_fast
    if len(chosen) < target_count:
        for o in sorted_offers:
            if not is_chosen(o):
                chosen.append(o)
                o._site_top_role = "fallback"
                if len(chosen) >= target_count:
                    break

    # Результат отсортирован по цене
    chosen.sort(key=lambda o: o.price)
    return chosen
