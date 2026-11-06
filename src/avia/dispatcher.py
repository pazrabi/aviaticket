"""Модуль диспетчеризации и общей раскладки телефонов по сайтам для живого поиска."""
from __future__ import annotations

from collections import defaultdict
import logging
from typing import Any, Callable, Sequence

from .config import Settings, SiteConfig

logger = logging.getLogger("avia.dispatcher")


def _get_default_screen_state(settings: Settings) -> dict[str, bool | None]:
    """Извлекает текущее состояние экрана (screen_on) из PhoneRegistry."""
    state: dict[str, bool | None] = {}
    try:
        from .droidhub import _get_registry

        reg = _get_registry(settings)
        if reg is not None:
            snapshots = getattr(reg, "_snapshots", None)
            if isinstance(snapshots, dict):
                lock = getattr(reg, "_lock", None)
                if lock is not None:
                    with lock:
                        items = list(snapshots.items())
                else:
                    items = list(snapshots.items())
                for dev_id, snap in items:
                    if isinstance(snap, dict):
                        s_on = snap.get("screen_on")
                    else:
                        s_on = getattr(snap, "screen_on", None)
                    if s_on is not None:
                        state[str(dev_id).strip()] = bool(s_on)
    except Exception as e:
        logger.warning("Не удалось получить screen_state из PhoneRegistry: %s", e)
        state = {}
    return state


def _calc_pair_cost(
    settings: Settings,
    speed_table: Any,
    site_name: str,
    dev: str,
    screen_state: dict[str, bool | None],
    now: float | None = None,
) -> float:
    """Вычисляет стоимость пары (сайт, телефон) с учётом поправки на пробуждение экрана."""
    base_cost: float | None = None
    if speed_table is not None:
        try:
            st = speed_table.get_stats(site_name, dev, "live", now=now)
        except TypeError:
            st = speed_table.get_stats(site_name, dev, "live")
        except Exception:
            st = None
        if st is not None:
            base_cost = getattr(st, "cost", None)

    if base_cost is None and speed_table is not None:
        try:
            site_med = speed_table.get_site_median(site_name, "live")
            if site_med is not None:
                mult = None
                if hasattr(speed_table, "_phone_multipliers"):
                    mult = (
                        speed_table._phone_multipliers.get((dev, "live"))
                        or speed_table._phone_overall_multipliers.get(dev, 1.0)
                    )
                if mult is None:
                    mult = 1.0
                base_cost = round(float(site_med) * float(mult), 2)
        except Exception:
            base_cost = None

    if base_cost is None:
        base_cost = 40.0

    wake_seconds = float(getattr(settings, "phone_wake_seconds", 5.0))
    # Если телефон сейчас с выключенным экраном (screen_state[dev] is False),
    # добавить settings.phone_wake_seconds; если состояние неизвестно (None/нет) — не добавлять.
    if screen_state.get(dev) is False:
        return round(base_cost + wake_seconds, 2)
    return round(base_cost, 2)


def _check_pair_status(
    settings: Settings,
    speed_table: Any,
    site_name: str,
    dev: str,
    now: float | None = None,
) -> tuple[bool, bool]:
    """Возвращает (is_primary, is_quarantine) для пары (сайт, телефон).

    Пара допускается в «основные», только если не в карантине,
    p_lower >= settings.speed_live_min_p_lower и n >= settings.speed_min_samples.
    """
    min_samples = getattr(settings, "speed_min_samples", 5)
    min_p_lower = getattr(settings, "speed_live_min_p_lower", 0.6)

    in_quarantine = False
    is_primary = False

    if speed_table is not None:
        try:
            pair_st = speed_table.get_pair_status(site_name, dev, now=now)
        except TypeError:
            pair_st = speed_table.get_pair_status(site_name, dev)
        except Exception:
            pair_st = None

        if pair_st is not None:
            if getattr(pair_st, "in_quarantine", False) or getattr(pair_st, "awaiting_trial", False):
                in_quarantine = True

        try:
            st = speed_table.get_stats(site_name, dev, "live", now=now)
        except TypeError:
            st = speed_table.get_stats(site_name, dev, "live")
        except Exception:
            st = None

        if not in_quarantine and st is not None:
            n = getattr(st, "n", 0)
            p_lower = getattr(st, "p_lower", 0.0)
            if n >= min_samples and p_lower >= min_p_lower:
                is_primary = True

    return is_primary, in_quarantine


def _branch_and_bound_distinct(
    sites: list[SiteConfig],
    primary_map: dict[str, list[str]],
    cost_map: dict[str, dict[str, float]],
) -> dict[str, str] | None:
    """Поиск назначения телефонов без повторений (минимум максимума, при равенстве — минимум суммы)."""
    # Сортируем сайты по числу кандидатов (эвристика MRV)
    sorted_sites = sorted(sites, key=lambda s: len(primary_map[s.name]))

    best_assignment: dict[str, str] | None = None
    best_max: float = float("inf")
    best_sum: float = float("inf")

    # Кандидаты каждого сайта по возрастанию стоимости
    sorted_cands: dict[str, list[str]] = {}
    for s in sorted_sites:
        sorted_cands[s.name] = sorted(
            primary_map[s.name],
            key=lambda d: cost_map[s.name][d],
        )

    n_sites = len(sorted_sites)
    used_phones: set[str] = set()
    current_assignment: dict[str, str] = {}

    def dfs(idx: int, cur_max: float, cur_sum: float) -> None:
        nonlocal best_assignment, best_max, best_sum
        if idx == n_sites:
            c_max = round(cur_max, 4)
            c_sum = round(cur_sum, 4)
            b_max = round(best_max, 4)
            b_sum = round(best_sum, 4)
            if (c_max < b_max) or (c_max == b_max and c_sum < b_sum):
                best_max = cur_max
                best_sum = cur_sum
                best_assignment = current_assignment.copy()
            return

        s_name = sorted_sites[idx].name
        cands = sorted_cands[s_name]

        for d in cands:
            if d in used_phones:
                continue
            cost = cost_map[s_name][d]
            next_max = max(cur_max, cost)
            next_sum = cur_sum + cost

            if round(next_max, 4) > round(best_max, 4):
                continue
            if round(next_max, 4) == round(best_max, 4) and round(next_sum, 4) >= round(best_sum, 4):
                continue

            used_phones.add(d)
            current_assignment[s_name] = d
            dfs(idx + 1, next_max, next_sum)
            del current_assignment[s_name]
            used_phones.remove(d)

    dfs(0, 0.0, 0.0)
    return best_assignment


def _assign_with_repetition(
    sites: list[SiteConfig],
    primary_map: dict[str, list[str]],
    cost_map: dict[str, dict[str, float]],
) -> dict[str, str]:
    """Назначение телефонов, когда сайтов больше чем подходящих телефонов или есть конфликты.

    Сначала свободные телефоны раздаются сайтам, лишние сайты получают телефон с наименьшей
    добавленной нагрузкой (телефон может повторяться).
    """
    sorted_sites = sorted(sites, key=lambda s: len(primary_map[s.name]))
    phone_assignments: dict[str, list[str]] = defaultdict(list)
    site_assignment: dict[str, str] = {}

    for s in sorted_sites:
        s_name = s.name
        cands = primary_map[s_name]
        unused = [d for d in cands if not phone_assignments[d]]
        if unused:
            best_d = min(unused, key=lambda d: cost_map[s_name][d])
        else:
            # Лишний сайт получает телефон с наименьшей добавленной нагрузкой
            def load_key(d: str) -> tuple[float, float, int]:
                cur_load = sum(cost_map[prev_s][d] for prev_s in phone_assignments[d])
                added_load = cost_map[s_name][d]
                return (round(cur_load + added_load, 4), round(added_load, 4), len(phone_assignments[d]))

            best_d = min(cands, key=load_key)

        site_assignment[s_name] = best_d
        phone_assignments[best_d].append(s_name)

    return site_assignment


def plan_live_assignment(
    settings: Settings,
    sites: Sequence[SiteConfig],
    now: float | None = None,
    *,
    speed_table: Any | None = None,
    screen_state: dict[str, bool | None] | None = None,
    candidates_fn: Callable[..., Sequence[str]] | None = None,
) -> dict[str, list[str]]:
    """Строит общую раскладку «сайт → упорядоченный список телефонов» для живого поиска.

    Каждому включённому browser-сайту с указанным phone назначается один основной телефон
    так, чтобы самый медленный сайт поиска был как можно быстрее (min-max ожидаемого времени C,
    при равенстве — min суммы).
    """
    if speed_table is None:
        try:
            from .speed import get_speed_table

            speed_table = get_speed_table(settings)
        except Exception as e:
            logger.warning("Не удалось получить SpeedTable: %s", e)
            speed_table = None

    if screen_state is None:
        screen_state = _get_default_screen_state(settings)

    if candidates_fn is None:
        from .droidhub import candidates as default_candidates_fn

        candidates_fn = default_candidates_fn

    eligible_sites: list[SiteConfig] = []
    for s in sites:
        if not getattr(s, "enabled", True):
            continue
        if getattr(s, "source", "browser") != "browser":
            continue
        if not (getattr(s, "phone", "") or "").strip():
            continue
        eligible_sites.append(s)

    if not eligible_sites:
        return {}

    site_cands: dict[str, list[str]] = {}
    site_primary: dict[str, list[str]] = {}
    site_backup: dict[str, list[str]] = {}
    site_quarantine: dict[str, list[str]] = {}
    site_cost_map: dict[str, dict[str, float]] = {}

    for s in eligible_sites:
        s_name = s.name
        try:
            cands_raw = candidates_fn(settings, s, priority=0)
        except TypeError:
            cands_raw = candidates_fn(settings, s)
        cands = [str(c).strip() for c in cands_raw if str(c).strip()] if cands_raw else []
        site_cands[s_name] = cands

        primaries: list[str] = []
        backups: list[str] = []
        quarantines: list[str] = []
        costs: dict[str, float] = {}

        for dev in cands:
            cost = _calc_pair_cost(settings, speed_table, s_name, dev, screen_state, now=now)
            costs[dev] = cost
            is_prim, is_quar = _check_pair_status(settings, speed_table, s_name, dev, now=now)
            if is_quar:
                quarantines.append(dev)
            elif is_prim:
                primaries.append(dev)
            else:
                backups.append(dev)

        site_primary[s_name] = primaries
        site_backup[s_name] = backups
        site_quarantine[s_name] = quarantines
        site_cost_map[s_name] = costs

    result: dict[str, list[str]] = {}
    active_assignment_sites: list[SiteConfig] = []

    for s in eligible_sites:
        s_name = s.name
        if not site_primary[s_name]:
            # Сайт без «основных» кандидатов получает пустого основного и просто свой прежний список
            result[s_name] = list(site_cands[s_name])
        else:
            active_assignment_sites.append(s)

    if not active_assignment_sites:
        return result

    all_primary_phones = {
        d for s in active_assignment_sites for d in site_primary[s.name]
    }

    assignment: dict[str, str] | None = None
    if len(active_assignment_sites) <= len(all_primary_phones):
        assignment = _branch_and_bound_distinct(active_assignment_sites, site_primary, site_cost_map)

    if assignment is None:
        assignment = _assign_with_repetition(active_assignment_sites, site_primary, site_cost_map)

    for s in active_assignment_sites:
        s_name = s.name
        assigned_dev = assignment[s_name]
        costs = site_cost_map[s_name]

        other_primaries = [d for d in site_primary[s_name] if d != assigned_dev]
        other_primaries.sort(key=lambda d: (costs.get(d, 40.0), site_cands[s_name].index(d)))

        backups = list(site_backup[s_name])
        backups.sort(key=lambda d: (costs.get(d, 40.0), site_cands[s_name].index(d)))

        quarantines = list(site_quarantine[s_name])
        quarantines.sort(key=lambda d: (costs.get(d, 40.0), site_cands[s_name].index(d)))

        result[s_name] = [assigned_dev] + other_primaries + backups + quarantines

    return result
