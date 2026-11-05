"""Модуль маршрутизации телефонов по замерам скорости и надёжности (speed routing)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
import statistics
import threading
import time
from typing import Any, Sequence

from .config import Settings, SiteConfig
from .droidhub import PreferenceList

logger = logging.getLogger("avia.speed")


@dataclass
class SpeedStats:
    """Метрики скорости и надёжности для триплета (сайт, телефон, класс)."""
    site: str
    phone: str
    class_name: str  # "live" или "bg"
    n: int = 0
    ok_count: int = 0
    ok_rate: float = 0.0
    median_fetch: float | None = None
    consecutive_failures: int = 0
    last_failure_ts: float | None = None
    last_run_ts: float | None = None
    in_quarantine: bool = False
    awaiting_trial: bool = False


@dataclass
class PairStatus:
    """Статус пары (сайт, телефон) для карантина и исследования."""
    consecutive_failures: int = 0
    last_failure_ts: float | None = None
    in_quarantine: bool = False
    awaiting_trial: bool = False
    total_runs: int = 0


class SpeedTable:
    """Кэшированная в памяти таблица замеров скорости и статусов телефонов."""

    def __init__(
        self,
        metrics_path: Path | str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings
        if metrics_path is not None:
            self.metrics_path = Path(metrics_path)
        elif settings is not None and getattr(settings, "storage_dir", None) is not None:
            self.metrics_path = Path(settings.storage_dir) / "metrics.sqlite"
        else:
            self.metrics_path = Path("state") / "metrics.sqlite"

        self._lock = threading.Lock()
        self._stats: dict[tuple[str, str, str], SpeedStats] = {}
        self._pairs: dict[tuple[str, str], PairStatus] = {}
        self._site_medians: dict[tuple[str, str], float] = {}
        self._last_refresh: float = 0.0

    def invalidate(self) -> None:
        """Сбрасывает таймер кэша, форсируя обновление при следующем запросе."""
        with self._lock:
            self._last_refresh = 0.0

    def refresh(self, now: float | None = None) -> None:
        """Пересчитывает метрики из site_runs за последние speed_window_days."""
        curr_time = time.time() if now is None else float(now)
        settings = self.settings
        window_days = getattr(settings, "speed_window_days", 7) if settings else 7
        quarantine_failures = getattr(settings, "speed_quarantine_failures", 3) if settings else 3
        quarantine_seconds = getattr(settings, "speed_quarantine_seconds", 600) if settings else 600
        since_ts = curr_time - (window_days * 86400.0)

        if not self.metrics_path.exists() and str(self.metrics_path) != ":memory:":
            with self._lock:
                self._stats.clear()
                self._pairs.clear()
                self._site_medians.clear()
                self._last_refresh = time.monotonic()
            return

        conn = None
        try:
            conn = sqlite3.connect(self.metrics_path, timeout=5)
            conn.row_factory = sqlite3.Row
            with conn:
                # Проверяем наличие таблицы site_runs
                table_check = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='site_runs'"
                ).fetchone()
                if not table_check:
                    with self._lock:
                        self._stats.clear()
                        self._pairs.clear()
                        self._site_medians.clear()
                        self._last_refresh = time.monotonic()
                    return

                # Проверяем колонку priority
                cols = {row[1] for row in conn.execute("PRAGMA table_info(site_runs)").fetchall()}
                prio_col = "COALESCE(priority, 0) AS priority" if "priority" in cols else "0 AS priority"

                # Загружаем все замеры за окно
                cur = conn.execute(
                    f"""
                    SELECT id, ts, site, ok, partial, captcha, offers, fetch_seconds, device, {prio_col}
                    FROM site_runs
                    WHERE ts >= ? AND device IS NOT NULL AND device != ''
                    ORDER BY ts ASC, id ASC
                    """,
                    (since_ts,),
                )
                rows = cur.fetchall()

                # Для последовательных неудач берём последние запуски каждой пары (сайт, телефон)
                # даже если они были чуть раньше окна (до 100 последних записей на пару)
                all_recent_cur = conn.execute(
                    f"""
                    SELECT id, ts, site, ok, partial, captcha, offers, device, {prio_col}
                    FROM site_runs
                    WHERE device IS NOT NULL AND device != ''
                    ORDER BY ts DESC, id DESC
                    LIMIT 2000
                    """
                )
                recent_rows = all_recent_cur.fetchall()
        except Exception as e:
            logger.warning("Ошибка чтения метрик для таблицы скоростей (%s): %s", self.metrics_path, e)
            return
        finally:
            if conn is not None:
                conn.close()

        # 1. Расчёт последовательных неудач и карантина для каждой пары (сайт, телефон)
        pair_runs_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
        pair_total_counts: dict[tuple[str, str], int] = {}
        for r in recent_rows:
            dev = str(r["device"]).strip()
            s_name = str(r["site"]).strip()
            if not dev or not s_name:
                continue
            pair = (s_name, dev)
            pair_runs_map.setdefault(pair, []).append(r)

        new_pairs: dict[tuple[str, str], PairStatus] = {}
        for pair, p_rows in pair_runs_map.items():
            fail_count = 0
            last_fail_ts = None
            for r in p_rows:
                is_captcha = bool(r["captcha"])
                is_empty_partial = bool(r["partial"]) and int(r["offers"] or 0) == 0
                is_failed = (int(r["ok"]) == 0) or is_captcha or is_empty_partial
                if is_failed:
                    fail_count += 1
                    if last_fail_ts is None:
                        last_fail_ts = float(r["ts"])
                else:
                    break

            in_quarantine = False
            awaiting_trial = False
            if fail_count >= quarantine_failures and last_fail_ts is not None:
                if (curr_time - last_fail_ts) < quarantine_seconds:
                    in_quarantine = True
                else:
                    awaiting_trial = True

            new_pairs[pair] = PairStatus(
                consecutive_failures=fail_count,
                last_failure_ts=last_fail_ts,
                in_quarantine=in_quarantine,
                awaiting_trial=awaiting_trial,
                total_runs=len(p_rows),
            )

        # 2. Агрегация замеров по (сайт, телефон, класс) за окно
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for r in rows:
            dev = str(r["device"]).strip()
            s_name = str(r["site"]).strip()
            if not dev or not s_name:
                continue
            prio = r["priority"]
            c_name = "live" if (prio is None or int(prio) == 0) else "bg"
            key = (s_name, dev, c_name)
            grouped.setdefault(key, []).append(r)
            pair = (s_name, dev)
            pair_total_counts[pair] = pair_total_counts.get(pair, 0) + 1

        for pair, cnt in pair_total_counts.items():
            if pair in new_pairs:
                new_pairs[pair].total_runs = max(new_pairs[pair].total_runs, cnt)
            else:
                new_pairs[pair] = PairStatus(total_runs=cnt)

        new_stats: dict[tuple[str, str, str], SpeedStats] = {}
        site_class_fetches: dict[tuple[str, str], list[float]] = {}

        for key, r_list in grouped.items():
            s_name, dev, c_name = key
            n = len(r_list)
            ok_count = 0
            fetch_times: list[float] = []
            last_run_ts = 0.0

            for r in r_list:
                ts = float(r["ts"])
                if ts > last_run_ts:
                    last_run_ts = ts
                is_captcha = bool(r["captcha"])
                is_empty_partial = bool(r["partial"]) and int(r["offers"] or 0) == 0
                is_ok = bool(r["ok"]) and not is_captcha and not is_empty_partial

                if is_ok:
                    ok_count += 1
                    fs = r["fetch_seconds"]
                    if fs is not None and float(fs) > 0:
                        val = float(fs)
                        fetch_times.append(val)
                        site_class_fetches.setdefault((s_name, c_name), []).append(val)

            median_fetch = statistics.median(fetch_times) if fetch_times else None
            ok_rate = ok_count / n if n > 0 else 0.0

            pair_st = new_pairs.get((s_name, dev))
            consec_fail = pair_st.consecutive_failures if pair_st else 0
            last_fail_ts = pair_st.last_failure_ts if pair_st else None
            in_quarantine = pair_st.in_quarantine if pair_st else False
            awaiting_trial = pair_st.awaiting_trial if pair_st else False

            new_stats[key] = SpeedStats(
                site=s_name,
                phone=dev,
                class_name=c_name,
                n=n,
                ok_count=ok_count,
                ok_rate=round(ok_rate, 4),
                median_fetch=round(median_fetch, 2) if median_fetch is not None else None,
                consecutive_failures=consec_fail,
                last_failure_ts=last_fail_ts,
                last_run_ts=last_run_ts if last_run_ts > 0 else None,
                in_quarantine=in_quarantine,
                awaiting_trial=awaiting_trial,
            )

        # 3. Медианы по сайту и классу
        new_site_medians: dict[tuple[str, str], float] = {}
        for sc_key, times in site_class_fetches.items():
            if times:
                new_site_medians[sc_key] = round(statistics.median(times), 2)

        # 4. Обновляем кэш
        with self._lock:
            self._stats = new_stats
            self._pairs = new_pairs
            self._site_medians = new_site_medians
            self._last_refresh = time.monotonic()

    def _ensure_fresh(self, now: float | None = None) -> None:
        refresh_sec = getattr(self.settings, "speed_refresh_seconds", 300) if self.settings else 300
        mono = time.monotonic()
        if (mono - self._last_refresh) >= refresh_sec:
            self.refresh(now=now)

    def get_stats(
        self,
        site: str,
        phone: str,
        class_name: str,
        now: float | None = None,
    ) -> SpeedStats:
        """Возвращает метрики для тройки (сайт, телефон, класс)."""
        self._ensure_fresh(now=now)
        clean_dev = str(phone).strip()
        clean_site = str(site).strip()
        clean_class = "bg" if class_name == "bg" else "live"

        with self._lock:
            st = self._stats.get((clean_site, clean_dev, clean_class))
            if st is not None:
                return st
            pair_st = self._pairs.get((clean_site, clean_dev))
            consec_fail = pair_st.consecutive_failures if pair_st else 0
            last_fail_ts = pair_st.last_failure_ts if pair_st else None
            in_quar = pair_st.in_quarantine if pair_st else False
            await_tr = pair_st.awaiting_trial if pair_st else False

            return SpeedStats(
                site=clean_site,
                phone=clean_dev,
                class_name=clean_class,
                n=0,
                ok_count=0,
                ok_rate=0.0,
                median_fetch=None,
                consecutive_failures=consec_fail,
                last_failure_ts=last_fail_ts,
                in_quarantine=in_quar,
                awaiting_trial=await_tr,
            )

    def get_pair_status(
        self,
        site: str,
        phone: str,
        now: float | None = None,
    ) -> PairStatus:
        """Возвращает статус карантина и неудач пары (сайт, телефон)."""
        self._ensure_fresh(now=now)
        clean_dev = str(phone).strip()
        clean_site = str(site).strip()
        with self._lock:
            st = self._pairs.get((clean_site, clean_dev))
            if st is not None:
                return st
            return PairStatus()

    def get_site_median(
        self,
        site: str,
        class_name: str,
        exclude_phone: str | None = None,
    ) -> float | None:
        """Медиана времени ответа по сайту среди телефонов."""
        clean_site = str(site).strip()
        clean_class = "bg" if class_name == "bg" else "live"
        with self._lock:
            if exclude_phone is None:
                return self._site_medians.get((clean_site, clean_class))

            fetches: list[float] = []
            for (s, p, c), st in self._stats.items():
                if s == clean_site and c == clean_class and p != exclude_phone:
                    if st.median_fetch is not None:
                        fetches.append(st.median_fetch)
            if fetches:
                return round(statistics.median(fetches), 2)
            return self._site_medians.get((clean_site, clean_class))

    def order_candidates(
        self,
        site: SiteConfig,
        base_candidates: Sequence[str],
        priority: int = 0,
        now: float | None = None,
    ) -> PreferenceList:
        """Сортирует список кандидатов по скорости и надёжности.

        - priority 0 (люди): быстрые первыми; ok_rate < 0.4 или карантин — в конец;
          медленнее лучшего в slow_ratio раз — в конец как запасные, patience = разница медиан;
          новые пары (n == 0) — в конец.
        - priority > 0 (подписки/фон): новые пары (n == 0) и пробные попытки после карантина — первыми;
          затем медленные из пригодных (медленные первыми, быстрые в конце);
          быстрые берутся, если медленные заняты дольше speed_bg_patience_seconds;
          непригодные (ok_rate < 0.4, карантин) — в самый конец.
        """
        cands = [str(c).strip() for c in base_candidates if str(c).strip()]
        if not cands:
            return PreferenceList([])
        if len(cands) == 1:
            return PreferenceList(cands, patience=float(getattr(site, "phone_patience_seconds", 0) or 0))

        self._ensure_fresh(now=now)
        settings = self.settings
        min_samples = getattr(settings, "speed_min_samples", 5) if settings else 5
        slow_ratio = getattr(settings, "speed_slow_ratio", 3.0) if settings else 3.0
        bg_patience = getattr(settings, "speed_bg_patience_seconds", 60) if settings else 60
        site_name = getattr(site, "name", "")

        if priority == 0:
            # -------------------------------------------------------------
            # PRIORITY 0: ЛЮДИ (быстрые первыми)
            # -------------------------------------------------------------
            site_med = self.get_site_median(site_name, "live")
            bad_candidates: list[str] = []
            new_candidates: list[str] = []
            eligible: list[tuple[str, float | None, int, int]] = []

            for idx, dev in enumerate(cands):
                pair_st = self.get_pair_status(site_name, dev, now=now)
                st = self.get_stats(site_name, dev, "live", now=now)
                bg_st = self.get_stats(site_name, dev, "bg", now=now)

                is_bad_rate = (st.n >= min_samples and st.ok_rate < 0.4) or (
                    st.n < min_samples and bg_st.n >= min_samples and bg_st.ok_rate < 0.4
                )

                if pair_st.in_quarantine or pair_st.awaiting_trial:
                    # В карантине или ждёт пробной попытки от фона (люди пробным не становятся)
                    bad_candidates.append(dev)
                elif is_bad_rate:
                    # Непригоден по доле успеха
                    bad_candidates.append(dev)
                elif (st.n + bg_st.n == 0) and pair_st.total_runs == 0:
                    # Новая пара без замеров
                    new_candidates.append(dev)
                else:
                    # Пригодный телефон
                    if st.n >= min_samples and st.median_fetch is not None:
                        est = st.median_fetch
                    else:
                        est = site_med if site_med is not None else st.median_fetch
                    eligible.append((dev, est, st.n, idx))

            # Если у всех телефонов нет данных (n == 0), сохраняем исходный порядок (Req 2a)
            if not eligible and not bad_candidates:
                orig_patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
                if isinstance(cands, PreferenceList):
                    return PreferenceList(cands, patience=orig_patience)
                return list(cands)

            if eligible:
                # Находим лучший (наименьший) медианный замер среди телефонов с достаточным n
                measured = [item for item in eligible if item[2] >= min_samples and item[1] is not None]
                if measured:
                    best_med = min(item[1] for item in measured)
                elif any(item[1] is not None for item in eligible):
                    best_med = min(item[1] for item in eligible if item[1] is not None)
                else:
                    best_med = None

                fast_list: list[tuple[str, float | None, int, int]] = []
                slow_list: list[tuple[str, float | None, int, int]] = []

                for item in eligible:
                    dev, est, n_runs, orig_idx = item
                    if best_med is not None and est is not None and est > (best_med * slow_ratio):
                        # Медленнее лучшего больше чем в slow_ratio раз → в конец (запасные)
                        slow_list.append(item)
                    else:
                        fast_list.append(item)

                # Сортируем быстрые по est_median возрастающе (быстрые первыми)
                fast_list.sort(key=lambda x: (0 if x[1] is not None else 1, x[1] if x[1] is not None else 0, x[3]))
                # Сортируем медленные запасные по est_median возрастающе
                slow_list.sort(key=lambda x: (0 if x[1] is not None else 1, x[1] if x[1] is not None else 0, x[3]))

                fast_names = [x[0] for x in fast_list]
                slow_names = [x[0] for x in slow_list]

                # Люди новые пары не пробуют, кроме как единственный вариант (Req 4)
                # Итоговый порядок: быстрые + медленные запасные + новые пары + непригодные/карантин
                ordered = fast_names + slow_names + new_candidates + bad_candidates

                if slow_names and fast_names:
                    min_fast = min(x[1] for x in fast_list if x[1] is not None)
                    min_slow = min(x[1] for x in slow_list if x[1] is not None)
                    patience = max(0.0, round(min_slow - min_fast, 1))
                    preferred_count = len(fast_names)
                else:
                    patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
                    preferred_count = 1

                return PreferenceList(ordered, patience=patience, preferred_count=preferred_count)
            else:
                # Только новые пары или непригодные
                ordered = new_candidates + bad_candidates
                orig_patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
                return PreferenceList(ordered, patience=orig_patience, preferred_count=1)

        else:
            # -------------------------------------------------------------
            # PRIORITY > 0: ПОДПИСКИ И ФОН (медленные первыми, исследование новых)
            # -------------------------------------------------------------
            site_med = self.get_site_median(site_name, "bg") or self.get_site_median(site_name, "live")
            bad_candidates = []
            trial_candidates: list[str] = []
            exploratory_candidates: list[str] = []
            eligible = []

            for idx, dev in enumerate(cands):
                pair_st = self.get_pair_status(site_name, dev, now=now)
                st = self.get_stats(site_name, dev, "bg", now=now)
                live_st = self.get_stats(site_name, dev, "live", now=now)

                is_bad_rate = (st.n >= min_samples and st.ok_rate < 0.4) or (
                    st.n < min_samples and live_st.n >= min_samples and live_st.ok_rate < 0.4
                )

                if pair_st.in_quarantine or is_bad_rate:
                    # В активном карантине (< 10 минут) или непригоден (ok_rate < 0.4)
                    bad_candidates.append(dev)
                elif pair_st.awaiting_trial:
                    # После карантина пара получает одну пробную попытку ТОЛЬКО от фона (Req 3)
                    trial_candidates.append(dev)
                elif (st.n + live_st.n == 0) and pair_st.total_runs == 0:
                    # Новая пара без замеров (n == 0): фон исследует первыми (Req 4)
                    exploratory_candidates.append(dev)
                else:
                    # Пригодный телефон
                    if st.n >= min_samples and st.median_fetch is not None:
                        est = st.median_fetch
                    elif live_st.n >= min_samples and live_st.median_fetch is not None:
                        est = live_st.median_fetch
                    else:
                        est = site_med if site_med is not None else (st.median_fetch or live_st.median_fetch)
                    eligible.append((dev, est, st.n, idx))

            # Сортируем пригодные: ОБРАТНЫЙ ПОРЯДОК (медленные первыми, быстрые в конце)
            # est убывающе: None в самый конец пригодных
            eligible.sort(key=lambda x: (0 if x[1] is not None else 1, -(x[1] or 0), x[3]))

            # Разделяем медленные и быстрые телефоны для применения bg_patience_seconds
            measured_ests = [x[1] for x in eligible if x[1] is not None]
            if len(measured_ests) >= 2:
                fastest_est = min(measured_ests)
                slowest_est = max(measured_ests)
                if slowest_est > fastest_est:
                    # Медленные: est > fastest_est или est is None; быстрые: est <= fastest_est
                    slow_list = [x for x in eligible if x[1] is None or x[1] > fastest_est]
                    fast_list = [x for x in eligible if x[1] is not None and x[1] <= fastest_est]
                else:
                    slow_list = list(eligible)
                    fast_list = []
            else:
                slow_list = list(eligible)
                fast_list = []

            slow_names = [x[0] for x in slow_list]
            fast_names = [x[0] for x in fast_list]

            # Исследовательские пары (пробные после карантина + новые без замеров) идут ПЕРВЫМИ
            lead_names = trial_candidates + exploratory_candidates

            # Непригодные (ok_rate < 0.4 или в карантине) — в самый конец
            ordered = lead_names + slow_names + fast_names + bad_candidates

            # Терпение фоновых задач: быстрые берутся только если медленные/исследовательские
            # заняты дольше bg_patience_seconds (Req 2c)
            if fast_names and (lead_names or slow_names):
                patience = float(bg_patience)
                preferred_count = len(lead_names) + len(slow_names)
            else:
                patience = 0.0
                preferred_count = len(ordered)

            return PreferenceList(ordered, patience=patience, preferred_count=preferred_count)


# Глобальные объекты таблиц скоростей на хранилище
_speed_tables: dict[Path, SpeedTable] = {}
_speed_lock = threading.Lock()


def get_speed_table(settings: Settings | None = None) -> SpeedTable:
    """Возвращает singleton таблицы скоростей для заданных настроек."""
    if settings is not None and getattr(settings, "storage_dir", None) is not None:
        path = Path(settings.storage_dir) / "metrics.sqlite"
    else:
        path = Path("state") / "metrics.sqlite"

    with _speed_lock:
        table = _speed_tables.get(path)
        if table is None or table.settings is not settings:
            table = SpeedTable(metrics_path=path, settings=settings)
            _speed_tables[path] = table
        return table


def reset_speed_tables() -> None:
    """Сброс кэшей таблиц скоростей (для изоляции тестов)."""
    with _speed_lock:
        _speed_tables.clear()
