"""Управление стадиями сайтов (trial / live).

Сайты со стадией trial работают только в фоне и для подписок,
но не отдаются пользователям в живом поиске и не попадают в уведомления подписок.
При накоплении достаточного числа успешных проверок на разных телефонах
сайт готов к переводу в live.
"""
from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

log = logging.getLogger("avia.stage")

TRIAL_MIN_RUNS: int = 20
TRIAL_MIN_OK_SHARE: float = 0.9
TRIAL_MIN_PHONES: int = 2


def trial_stats(site_name: str, metrics: Any, now: float | None = None) -> dict[str, Any]:
    """Возвращает статистику запусков сайта за последние 24 ч для оценки готовности к live."""
    empty_res: dict[str, Any] = {
        "runs": 0,
        "ok_count": 0,
        "ok_share": 0.0,
        "phones": set(),
        "ready": False,
    }
    if metrics is None:
        return empty_res

    now_ts = time.time() if now is None else float(now)
    since_ts = now_ts - 86400.0

    try:
        if hasattr(metrics, "_connect"):
            with metrics._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT ok, offers, device
                    FROM site_runs
                    WHERE site = ? AND ts >= ?
                    """,
                    (site_name, since_ts),
                )
                rows = cur.fetchall()
        elif isinstance(metrics, sqlite3.Connection):
            cur = metrics.execute(
                """
                SELECT ok, offers, device
                FROM site_runs
                WHERE site = ? AND ts >= ?
                """,
                (site_name, since_ts),
            )
            rows = cur.fetchall()
        elif isinstance(metrics, (str, Path)):
            with sqlite3.connect(metrics) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT ok, offers, device
                    FROM site_runs
                    WHERE site = ? AND ts >= ?
                    """,
                    (site_name, since_ts),
                )
                rows = cur.fetchall()
        else:
            log.warning("Неизвестный тип metrics в trial_stats: %r", type(metrics))
            return empty_res
    except Exception as e:
        log.warning("Ошибка чтения site_runs для сайта %s: %s", site_name, e)
        return empty_res

    runs = len(rows)
    if runs == 0:
        return empty_res

    ok_count = 0
    phones: set[str] = set()
    for r in rows:
        try:
            ok_val = bool(r["ok"] if hasattr(r, "__getitem__") else getattr(r, "ok", 0))
            offers_val = int(r["offers"] if hasattr(r, "__getitem__") else getattr(r, "offers", 0) or 0)
            dev_val = r["device"] if hasattr(r, "__getitem__") else getattr(r, "device", None)
        except Exception as e:
            log.warning("Ошибка разбора строки site_runs для %s: %s", site_name, e)
            continue

        if ok_val and offers_val > 0:
            ok_count += 1
        if dev_val:
            dev_str = str(dev_val).strip()
            if dev_str and dev_str.lower() != "unknown":
                phones.add(dev_str)

    ok_share = (ok_count / runs) if runs > 0 else 0.0
    ready = (
        runs >= TRIAL_MIN_RUNS
        and ok_share >= TRIAL_MIN_OK_SHARE
        and len(phones) >= TRIAL_MIN_PHONES
    )
    return {
        "runs": runs,
        "ok_count": ok_count,
        "ok_share": ok_share,
        "phones": phones,
        "ready": ready,
    }


def trial_ready(site_name: str, metrics: Any, now: float | None = None) -> bool:
    """True, если за последние 24 ч по site_runs >= 20 запусков, ok с офферами >= 90% и >= 2 разных телефонов."""
    try:
        stats = trial_stats(site_name, metrics, now)
        return bool(stats.get("ready", False))
    except Exception as e:
        log.warning("Ошибка проверки trial_ready для сайта %s: %s", site_name, e)
        return False
