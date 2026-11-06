"""Правило остановки живого поиска по стабильному минимуму (PLAN_SPEED шаг 5)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StopParams:
    """Параметры правила остановки поиска."""
    stable_seconds: float = 8.0
    min_offers: int = 1
    cap_seconds: float = 45.0


_cache_path: Path | None = None
_cache_mtime: float | None = None
_cache_data: dict[str, Any] = {}


def clear_cache() -> None:
    """Сброс кеша правил остановки (для изоляции тестов)."""
    global _cache_path, _cache_mtime, _cache_data
    _cache_path = None
    _cache_mtime = None
    _cache_data = {}


def decide(state: Any, params: StopParams) -> str | None:
    """Определяет причину остановки живого поиска на основе текущего состояния.

    Порядок проверки:
    1. captcha -> 'captcha' (не решает правило, возвращает 'captcha', если captcha истинна)
    2. done -> 'done' (wait_for_seen истинно И есть хотя бы min_offers офферов)
    3. stable -> 'stable' (best_min есть, n_offers >= min_offers и elapsed - t_last_improve >= stable_seconds)
    4. quiet -> 'quiet' (network_quiet И best_min есть)
    5. job_done -> 'job_done' (job_state in ('done', 'failed') без остальных причин)
    6. cap -> 'cap' (elapsed >= cap_seconds)
    7. None (ждать дальше)
    """
    def _val(key: str, default: Any = None) -> Any:
        if isinstance(state, dict):
            return state.get(key, default)
        return getattr(state, key, default)

    # 1. captcha — не решает правило, вернуть 'captcha' если captcha истинна
    if bool(_val("captcha", False)):
        return "captcha"

    wait_for_seen = bool(_val("wait_for_seen", False))
    n_offers = int(_val("n_offers", 0) or 0)
    min_offers = getattr(params, "min_offers", 1)

    # 2. done — wait_for_seen истинно И есть хотя бы min_offers офферов
    if wait_for_seen and n_offers >= min_offers:
        return "done"

    best_min = _val("best_min")
    elapsed = float(_val("elapsed", 0.0) or 0.0)
    t_last_improve = _val("t_last_improve")
    stable_seconds = float(getattr(params, "stable_seconds", 8.0))

    # 3. stable — best_min есть, n_offers >= min_offers и elapsed − t_last_improve >= stable_seconds
    if (
        best_min is not None
        and n_offers >= min_offers
        and t_last_improve is not None
        and (elapsed - float(t_last_improve)) >= stable_seconds
    ):
        return "stable"

    # 4. quiet — network_quiet И best_min есть
    network_quiet = bool(_val("network_quiet", False))
    if network_quiet and best_min is not None:
        return "quiet"

    # 5. job_done — state=done или failed без остальных причин
    job_state = str(_val("job_state", "running") or "running")
    if job_state in ("done", "failed"):
        return "job_done"

    # 6. cap — elapsed >= cap_seconds
    cap_seconds = getattr(params, "cap_seconds", None)
    if cap_seconds is not None and elapsed >= float(cap_seconds):
        return "cap"

    return None


def stable_seconds_for(site_name: str, settings: Any = None) -> float:
    """Возвращает время стабильности (сек) для сайта.

    Берёт значение из файла state/stop_rules.json (формат {"<site>": {"stable_seconds": 6.5}}),
    если файл или значение отсутствует — settings.stop_stable_seconds (по умолчанию 8.0).
    Кеш файла по mtime. Любая ошибка чтения = значение по умолчанию.
    """
    default_val = float(getattr(settings, "stop_stable_seconds", 8.0) if settings is not None else 8.0)
    try:
        storage_dir = getattr(settings, "storage_dir", None)
        if storage_dir is not None:
            file_path = Path(storage_dir) / "stop_rules.json"
        else:
            file_path = Path("state/stop_rules.json")

        if not file_path.is_file():
            return default_val

        mtime = file_path.stat().st_mtime
        global _cache_path, _cache_mtime, _cache_data
        if _cache_path == file_path and _cache_mtime == mtime:
            data = _cache_data
        else:
            text = file_path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                return default_val
            _cache_path = file_path
            _cache_mtime = mtime
            _cache_data = data

        site_entry = data.get(site_name)
        if isinstance(site_entry, dict):
            val = site_entry.get("stable_seconds")
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return float(val)
        return default_val
    except Exception:
        return default_val
