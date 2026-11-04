"""Мониторинг работоспособности парсеров (parser watch) и оповещения администраторов."""
from __future__ import annotations

from html import escape
import logging
import time
from typing import Any

from .config import Settings
from .metrics import MetricsStore

log = logging.getLogger("avia.parser_watch")

STATUS_OK = "ок"
STATUS_DEGRADED = "деградирует"
STATUS_BROKEN = "сломан"
STATUS_FEW_RUNS = "мало данных"


def evaluate_status(
    runs: int,
    ok: int,
    partial: int,
    min_runs: int = 8,
    min_ok_pct: float = 60.0,
    max_incomplete_pct: float = 60.0,
) -> str:
    """Вычисляет статус сайта ('ок', 'деградирует', 'сломан', 'мало данных')."""
    if runs < min_runs:
        return STATUS_FEW_RUNS
    ok_pct = (ok / runs) * 100.0 if runs > 0 else 0.0
    partial_pct = (partial / runs) * 100.0 if runs > 0 else 0.0
    if ok_pct < min_ok_pct:
        return STATUS_BROKEN
    if partial_pct > max_incomplete_pct:
        return STATUS_DEGRADED
    return STATUS_OK


def get_parser_statuses(
    metrics: MetricsStore,
    settings: Settings,
    now: float | None = None,
) -> dict[str, str]:
    """Возвращает статус парсеров для раздела 'Парсинг' панели /admin."""
    try:
        now_ts = time.time() if now is None else float(now)
        since_ts = now_ts - settings.parser_watch_hours * 3600.0
        stats = metrics.watch_stats(since_ts)
        statuses: dict[str, str] = {}
        # Проверяем сайты из статистики или включённых в настройках
        all_sites = set(stats.keys())
        for s in settings.sites:
            if s.enabled and s.source == "browser":
                all_sites.add(s.name)

        for site in sorted(all_sites):
            st = stats.get(site, {"runs": 0, "ok": 0, "partial": 0})
            statuses[site] = evaluate_status(
                runs=st["runs"],
                ok=st["ok"],
                partial=st["partial"],
                min_runs=settings.parser_watch_min_runs,
                min_ok_pct=float(settings.parser_watch_min_ok_pct),
                max_incomplete_pct=float(settings.parser_watch_max_incomplete_pct),
            )
        return statuses
    except Exception:
        log.exception("ошибка получения статусов парсеров")
        return {}


def format_alert_message(
    site: str,
    status: str,
    ok_pct: float,
    partial_pct: float,
    hours: int,
    runs: int,
) -> str:
    """Форматирует короткое русское сообщение для админов (html.escape)."""
    esc_site = escape(site)
    ok_int = int(round(ok_pct))
    if status == STATUS_BROKEN:
        return f"⚠️ Сайт {esc_site}, ок {ok_int}% за {hours} ч ({runs} запусков) — сломан"
    elif status == STATUS_DEGRADED:
        part_int = int(round(partial_pct))
        return f"⚠️ Сайт {esc_site}, ок {ok_int}% за {hours} ч ({runs} запусков) — деградирует (неполных {part_int}%)"
    elif status == STATUS_OK:
        return f"✅ Сайт {esc_site} восстановился: ок {ok_int}% за {hours} ч ({runs} запусков)"
    return f"Сайт {esc_site}, ок {ok_int}% за {hours} ч ({runs} запусков)"


def check_parser_watch(
    metrics: MetricsStore,
    settings: Settings,
    now: float | None = None,
) -> list[str]:
    """Проверяет пробой парсеров и возвращает список сообщений для отправки админам.

    Соблюдает кулдаун и отправляет сообщение о восстановлении только один раз.
    """
    if not settings.parser_watch_enabled:
        return []

    messages: list[str] = []
    try:
        now_ts = time.time() if now is None else float(now)
        since_ts = now_ts - settings.parser_watch_hours * 3600.0
        cooldown_seconds = settings.parser_watch_cooldown_hours * 3600.0
        stats = metrics.watch_stats(since_ts)

        # Проверяем все сайты, по которым были запуски
        for site, st in sorted(stats.items()):
            runs = st["runs"]
            ok_cnt = st["ok"]
            part_cnt = st["partial"]

            # Если запусков мало — ничего не слать
            if runs < settings.parser_watch_min_runs:
                continue

            ok_pct = (ok_cnt / runs) * 100.0 if runs > 0 else 0.0
            part_pct = (part_cnt / runs) * 100.0 if runs > 0 else 0.0

            status = evaluate_status(
                runs=runs,
                ok=ok_cnt,
                partial=part_cnt,
                min_runs=settings.parser_watch_min_runs,
                min_ok_pct=float(settings.parser_watch_min_ok_pct),
                max_incomplete_pct=float(settings.parser_watch_max_incomplete_pct),
            )

            saved_state = metrics.get_watch_state(site)
            last_alert_ts = saved_state["last_alert_ts"] if saved_state else 0.0
            last_alert_status = saved_state["last_alert_status"] if saved_state else STATUS_OK

            if status in (STATUS_BROKEN, STATUS_DEGRADED):
                # Проверяем кулдаун
                time_since_last = now_ts - last_alert_ts
                is_cooldown = (last_alert_status in (STATUS_BROKEN, STATUS_DEGRADED)) and (time_since_last < cooldown_seconds)
                if not is_cooldown:
                    msg = format_alert_message(
                        site=site,
                        status=status,
                        ok_pct=ok_pct,
                        partial_pct=part_pct,
                        hours=settings.parser_watch_hours,
                        runs=runs,
                    )
                    messages.append(msg)
                    metrics.set_watch_state(
                        site=site,
                        last_status=status,
                        last_alert_ts=now_ts,
                        last_alert_status=status,
                    )
            elif status == STATUS_OK:
                # Если сайт ранее был сломан или деградировал — шлём "восстановился" один раз
                if last_alert_status in (STATUS_BROKEN, STATUS_DEGRADED):
                    msg = format_alert_message(
                        site=site,
                        status=STATUS_OK,
                        ok_pct=ok_pct,
                        partial_pct=part_pct,
                        hours=settings.parser_watch_hours,
                        runs=runs,
                    )
                    messages.append(msg)
                    metrics.set_watch_state(
                        site=site,
                        last_status=STATUS_OK,
                        last_alert_ts=now_ts,
                        last_alert_status=STATUS_OK,
                    )
                else:
                    metrics.set_watch_state(
                        site=site,
                        last_status=STATUS_OK,
                        last_alert_ts=last_alert_ts,
                        last_alert_status=last_alert_status,
                    )
    except Exception:
        log.exception("ошибка в check_parser_watch")

    return messages
