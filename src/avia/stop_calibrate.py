"""Калибровка времени стабильности (stable_seconds) правил остановки живого поиска."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Sequence
import uuid

logger = logging.getLogger(__name__)

DEFAULT_GRID: tuple[int, ...] = (3, 4, 5, 6, 7, 8, 10, 12, 15)


def _simulate_stop_price(improvements: list[tuple[float, float]], N: float) -> float:
    """Определяет цену в момент остановки по правилу стабильности N секунд."""
    for idx in range(len(improvements) - 1):
        t_curr, m_curr = improvements[idx]
        t_next, _ = improvements[idx + 1]
        if t_next - t_curr > N:
            # За время N не появилось следующего улучшения — поиск остановлен
            return m_curr
    return improvements[-1][1]


def _is_miss(
    improvements: list[tuple[float, float]],
    final_min: float,
    N: float,
    loss_pct: float,
) -> bool:
    """Проверяет, является ли исход промахом (цена при остановке > final_min на > loss_pct процентов)."""
    if len(improvements) <= 1:
        return False
    stop_price = _simulate_stop_price(improvements, N)
    if stop_price <= final_min:
        return False
    diff_pct = (stop_price - final_min) / final_min * 100.0
    return diff_pct > loss_pct


def calibrate_site(
    timelines: list[dict],
    *,
    grid: Sequence[int | float] = DEFAULT_GRID,
    max_miss_share: float = 0.02,
    loss_pct: float = 0.5,
    min_samples: int = 20,
    default: float = 8.0,
) -> dict[str, Any] | None:
    """Калибрует stable_seconds для одного сайта по хронологиям цен.

    Возвращает dict с ключами stable_seconds, samples, miss_share,
    или None, если валидных хронологий меньше min_samples.
    """
    valid_data: list[tuple[list[tuple[float, float]], float]] = []

    for tl in timelines:
        pts = tl.get("points")
        f_min = tl.get("final_min")
        if pts is None or f_min is None:
            continue
        if isinstance(pts, str):
            try:
                pts = json.loads(pts)
            except Exception:
                continue
        if not isinstance(pts, list) or len(pts) == 0:
            continue
        try:
            f_min_val = float(f_min)
            if f_min_val <= 0:
                continue
        except (ValueError, TypeError):
            continue

        clean_pts: list[tuple[float, float]] = []
        for p in pts:
            if isinstance(p, dict) and "t" in p and "min" in p:
                try:
                    t_val = float(p["t"])
                    m_val = float(p["min"])
                    if m_val > 0:
                        clean_pts.append((t_val, m_val))
                except (ValueError, TypeError):
                    continue

        if not clean_pts:
            continue

        clean_pts.sort(key=lambda x: x[0])

        improvements: list[tuple[float, float]] = []
        cur_min = float("inf")
        for t_val, m_val in clean_pts:
            if m_val < cur_min:
                cur_min = m_val
                improvements.append((t_val, m_val))

        valid_data.append((improvements, f_min_val))

    total_samples = len(valid_data)
    if total_samples < min_samples:
        return None

    if not grid:
        return {
            "stable_seconds": float(default),
            "samples": total_samples,
            "miss_share": 0.0,
        }

    sorted_grid = sorted(grid)
    for N in sorted_grid:
        misses = sum(
            1 for imps, f_min in valid_data if _is_miss(imps, f_min, float(N), loss_pct)
        )
        share = misses / total_samples
        if share <= max_miss_share:
            return {
                "stable_seconds": float(N),
                "samples": total_samples,
                "miss_share": round(share, 4),
            }

    # Если ни один не подошёл, берём максимум сетки
    max_N = sorted_grid[-1]
    max_misses = sum(
        1 for imps, f_min in valid_data if _is_miss(imps, f_min, float(max_N), loss_pct)
    )
    return {
        "stable_seconds": float(max_N),
        "samples": total_samples,
        "miss_share": round(max_misses / total_samples, 4),
    }


def calibrate_all(
    metrics: Any,
    days: int = 7,
    now: float | None = None,
    **kw: Any,
) -> dict[str, dict[str, Any]]:
    """Калибрует правила остановки по всем сайтам за последние days дней."""
    now_ts = time.time() if now is None else float(now)
    since_ts = now_ts - days * 86400.0
    all_timelines = metrics.timelines(since_ts=since_ts)

    by_site: dict[str, list[dict]] = {}
    for tl in all_timelines:
        site = tl.get("site")
        if not site:
            continue
        by_site.setdefault(site, []).append(tl)

    results: dict[str, dict[str, Any]] = {}
    for site, tls in sorted(by_site.items()):
        res = calibrate_site(tls, **kw)
        if res is not None:
            results[site] = res

    return results


def write_rules(path: Path | str, results: dict[str, dict[str, Any]]) -> None:
    """Атомарная запись результатов калибровки в stop_rules.json.

    Если results пуст, существующий файл не трогается.
    """
    if not results:
        return

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    now_ts = time.time()

    payload: dict[str, Any] = {}
    for site, item in sorted(results.items()):
        payload[site] = {
            "stable_seconds": item.get("stable_seconds"),
            "samples": item.get("samples"),
            "miss_share": item.get("miss_share"),
            "updated": item.get("updated", now_ts),
        }

    tmp_path = p.parent / f"{p.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def format_report(results: dict[str, Any]) -> str:
    """Короткая русская строка для панели админа."""
    if not results:
        return ""

    parts: list[str] = []
    for site in sorted(results.keys()):
        val = results[site]
        if isinstance(val, dict):
            sec = val.get("stable_seconds")
        else:
            sec = val
        if sec is None:
            continue
        try:
            sec_f = float(sec)
            sec_str = f"{int(sec_f)}" if sec_f.is_integer() else f"{sec_f:.1f}"
            parts.append(f"{site} {sec_str} с")
        except (ValueError, TypeError):
            continue

    if not parts:
        return ""
    return f"🎯 Время ожидания стабильной цены: {' · '.join(parts)}"
