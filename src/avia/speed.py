"""Модуль маршрутизации телефонов по замерам скорости и надёжности (speed routing)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import sqlite3
import statistics
import threading
import time
from typing import Any, Sequence

from .config import Settings, SiteConfig
from .droidhub import PreferenceList
from .statedb import db_path

logger = logging.getLogger("avia.speed")


def wilson_lower(p: float, n: int, z: float = 0.84) -> float:
    """Нижняя граница доверительного интервала Уилсона для вероятности p (z=0.84 ≈ 80%)."""
    if n <= 0:
        return 0.0
    denom = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    spread = z * math.sqrt(max(0.0, (p * (1.0 - p) + (z * z) / (4.0 * n)) / n))
    return max(0.0, (center - spread) / denom)


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
    cost: float | None = None
    p: float = 0.5
    p_lower: float = 0.0


@dataclass
class PairStatus:
    """Статус пары (сайт, телефон) для карантина и исследования."""
    consecutive_failures: int = 0
    last_failure_ts: float | None = None
    in_quarantine: bool = False
    awaiting_trial: bool = False
    total_runs: int = 0
    consecutive_captchas: int = 0
    quarantine_seconds: float = 600.0


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
            self.metrics_path = db_path(settings.storage_dir, "metrics")
        else:
            self.metrics_path = db_path(Path("state"), "metrics")

        self._lock = threading.RLock()
        # пересчёт из SQLite идёт в одном потоке за раз; остальные ждут его и берут свежую таблицу
        self._refresh_lock = threading.Lock()
        self._stats: dict[tuple[str, str, str], SpeedStats] = {}
        self._pairs: dict[tuple[str, str], PairStatus] = {}
        self._site_medians: dict[tuple[str, str], float] = {}
        self._site_overall_medians: dict[str, float] = {}
        self._phone_multipliers: dict[tuple[str, str], float] = {}
        self._phone_overall_multipliers: dict[str, float] = {}
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
        min_samples = getattr(settings, "speed_min_samples", 5) if settings else 5
        since_ts = curr_time - (window_days * 86400.0)

        if not self.metrics_path.exists() and str(self.metrics_path) != ":memory:":
            with self._lock:
                self._stats.clear()
                self._pairs.clear()
                self._site_medians.clear()
                self._site_overall_medians.clear()
                self._phone_multipliers.clear()
                self._phone_overall_multipliers.clear()
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
                        self._site_overall_medians.clear()
                        self._phone_multipliers.clear()
                        self._phone_overall_multipliers.clear()
                        self._last_refresh = time.monotonic()
                    return

                # Проверяем колонку priority
                cols = {row[1] for row in conn.execute("PRAGMA table_info(site_runs)").fetchall()}
                prio_col = "COALESCE(priority, 0) AS priority" if "priority" in cols else "0 AS priority"
                elapsed_col = "elapsed" if "elapsed" in cols else "0.0 AS elapsed"
                fetch_col = "fetch_seconds" if "fetch_seconds" in cols else "NULL AS fetch_seconds"

                # Загружаем все замеры за окно
                cur = conn.execute(
                    f"""
                    SELECT id, ts, site, ok, partial, captcha, offers, {fetch_col}, {elapsed_col}, device, {prio_col}
                    FROM site_runs
                    WHERE ts >= ? AND device IS NOT NULL AND device != ''
                    ORDER BY ts ASC, id ASC
                    """,
                    (since_ts,),
                )
                rows = cur.fetchall()

                # Для последовательных неудач и кулдауна берём последние запуски каждой пары (сайт, телефон)
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
        # Капча на паре — кулдаун 1 ч, повторная 3 ч, затем 6 ч, 12 ч (макс 12 ч).
        # 502/таймаут/пустая выдача — 3 провала подряд, 10 минут.
        CAPTCHA_COOLDOWNS = [3600.0, 10800.0, 21600.0, 43200.0]

        pair_runs_map: dict[tuple[str, str], list[sqlite3.Row]] = {}
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

            # Уровень кулдауна капчи считаем по числу подряд идущих капч-провалов пары с последнего запуска
            consecutive_captchas = 0
            for r in p_rows:
                if bool(r["captcha"]):
                    consecutive_captchas += 1
                else:
                    break

            in_quarantine = False
            awaiting_trial = False
            effective_quarantine_sec = float(quarantine_seconds)

            if consecutive_captchas >= 1 and last_fail_ts is not None:
                idx = min(consecutive_captchas - 1, len(CAPTCHA_COOLDOWNS) - 1)
                effective_quarantine_sec = CAPTCHA_COOLDOWNS[idx]
                if (curr_time - last_fail_ts) < effective_quarantine_sec:
                    in_quarantine = True
                else:
                    awaiting_trial = True
            elif fail_count >= quarantine_failures and last_fail_ts is not None:
                effective_quarantine_sec = float(quarantine_seconds)
                if (curr_time - last_fail_ts) < effective_quarantine_sec:
                    in_quarantine = True
                else:
                    awaiting_trial = True

            new_pairs[pair] = PairStatus(
                consecutive_failures=fail_count,
                last_failure_ts=last_fail_ts,
                in_quarantine=in_quarantine,
                awaiting_trial=awaiting_trial,
                total_runs=len(p_rows),
                consecutive_captchas=consecutive_captchas,
                quarantine_seconds=effective_quarantine_sec,
            )

        # 2. Агрегация замеров по (сайт, телефон, класс) за окно
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        pair_total_counts: dict[tuple[str, str], int] = {}

        site_class_fetches: dict[tuple[str, str], list[float]] = {}
        site_fetches: dict[str, list[float]] = {}
        phone_site_class_fetches: dict[tuple[str, str, str], list[float]] = {}
        phone_site_fetches: dict[tuple[str, str], list[float]] = {}

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

            is_captcha = bool(r["captcha"])
            is_empty_partial = bool(r["partial"]) and int(r["offers"] or 0) == 0
            is_ok = bool(r["ok"]) and not is_captcha and not is_empty_partial

            fs = r["fetch_seconds"]
            if fs is None or float(fs) <= 0:
                fs = r["elapsed"]
            if fs is not None and float(fs) > 0 and is_ok:
                val = float(fs)
                site_class_fetches.setdefault((s_name, c_name), []).append(val)
                site_fetches.setdefault(s_name, []).append(val)
                phone_site_class_fetches.setdefault((s_name, dev, c_name), []).append(val)
                phone_site_fetches.setdefault((s_name, dev), []).append(val)

        for pair, cnt in pair_total_counts.items():
            if pair in new_pairs:
                new_pairs[pair].total_runs = max(new_pairs[pair].total_runs, cnt)
            else:
                new_pairs[pair] = PairStatus(total_runs=cnt)

        # 3. Медианы по сайту
        new_site_medians: dict[tuple[str, str], float] = {}
        for sc_key, times in site_class_fetches.items():
            if times:
                new_site_medians[sc_key] = round(statistics.median(times), 2)

        new_site_overall_medians: dict[str, float] = {}
        for s_name, times in site_fetches.items():
            if times:
                new_site_overall_medians[s_name] = round(statistics.median(times), 2)

        # 4. Множители скорости телефонов
        # Множитель телефона = медиана по сайтам, где у телефона есть данные,
        # отношения (его медиана на сайте / медиана сайта по всем телефонам); если данных нет совсем — 1.0.
        all_phones: set[str] = (
            {dev for (s, dev, c) in grouped.keys()}
            | {dev for (s, dev) in new_pairs.keys()}
            | {dev for (s, dev) in phone_site_fetches.keys()}
        )

        new_phone_multipliers: dict[tuple[str, str], float] = {}
        new_phone_overall_multipliers: dict[str, float] = {}

        for dev in all_phones:
            overall_ratios: list[float] = []
            for s_name, s_med in new_site_overall_medians.items():
                p_fetches = phone_site_fetches.get((s_name, dev))
                if p_fetches and s_med > 0:
                    p_med = statistics.median(p_fetches)
                    overall_ratios.append(p_med / s_med)
            mult_overall = round(statistics.median(overall_ratios), 4) if overall_ratios else 1.0
            new_phone_overall_multipliers[dev] = mult_overall

            for c_name in ("live", "bg"):
                ratios: list[float] = []
                for (s_name, c), s_med in new_site_medians.items():
                    if c != c_name:
                        continue
                    p_fetches = phone_site_class_fetches.get((s_name, dev, c_name))
                    if p_fetches and s_med > 0:
                        p_med = statistics.median(p_fetches)
                        ratios.append(p_med / s_med)
                if ratios:
                    new_phone_multipliers[(dev, c_name)] = round(statistics.median(ratios), 4)
                else:
                    new_phone_multipliers[(dev, c_name)] = mult_overall

        # 5. Статистика пар (сайт, телефон, класс) по последним 30 запускам пары
        new_stats: dict[tuple[str, str, str], SpeedStats] = {}

        for key, r_list in grouped.items():
            s_name, dev, c_name = key
            recent_p_runs = r_list[-30:]
            n = len(recent_p_runs)
            ok_count = 0
            ok_fetches: list[float] = []
            fail_fetches: list[float] = []
            last_run_ts = 0.0

            for r in recent_p_runs:
                ts = float(r["ts"])
                if ts > last_run_ts:
                    last_run_ts = ts
                is_captcha = bool(r["captcha"])
                is_empty_partial = bool(r["partial"]) and int(r["offers"] or 0) == 0
                is_ok = bool(r["ok"]) and not is_captcha and not is_empty_partial

                fs = r["fetch_seconds"]
                if fs is None or float(fs) <= 0:
                    fs = r["elapsed"]
                val = float(fs) if (fs is not None and float(fs) > 0) else None

                if is_ok:
                    ok_count += 1
                    if val is not None:
                        ok_fetches.append(val)
                else:
                    if val is not None:
                        fail_fetches.append(val)

            for r in r_list:
                ts = float(r["ts"])
                if ts > last_run_ts:
                    last_run_ts = ts

            p = (ok_count + 1) / (n + 2)
            p_lower = wilson_lower(p, n, z=0.84)
            ok_rate = ok_count / n if n > 0 else 0.0

            site_med = new_site_medians.get((s_name, c_name)) or new_site_overall_medians.get(s_name)
            phone_mult = new_phone_multipliers.get((dev, c_name)) or new_phone_overall_multipliers.get(dev, 1.0)

            # t_ok = медиана fetch_seconds успешных (если успехов нет — медиана по сайту)
            if ok_fetches:
                t_ok = statistics.median(ok_fetches)
            else:
                t_ok = site_med

            # t_fail = среднее fetch_seconds провальных (если провалов нет — 0)
            if fail_fetches:
                t_fail = sum(fail_fetches) / len(fail_fetches)
            else:
                t_fail = 0.0

            # C = (p*t_ok + (1-p)*t_fail) / p
            if n >= min_samples:
                if t_ok is not None:
                    C_val = (p * t_ok + (1.0 - p) * t_fail) / p
                    cost = round(C_val, 2)
                else:
                    cost = None
            else:
                # Новая пара без данных (n < speed_min_samples): C = медиана сайта * множитель телефона
                if site_med is not None:
                    cost = round(site_med * phone_mult, 2)
                else:
                    cost = round(t_ok, 2) if t_ok is not None else None

            pair_st = new_pairs.get((s_name, dev))
            consec_fail = pair_st.consecutive_failures if pair_st else 0
            last_fail_ts = pair_st.last_failure_ts if pair_st else None
            in_quarantine = pair_st.in_quarantine if pair_st else False
            awaiting_trial = pair_st.awaiting_trial if pair_st else False

            # median_fetch: возвращает cost (или t_ok), обеспечивая показ ожидаемого времени C в админке
            med_fetch = cost if cost is not None else (round(t_ok, 2) if t_ok is not None else None)

            new_stats[key] = SpeedStats(
                site=s_name,
                phone=dev,
                class_name=c_name,
                n=n,
                ok_count=ok_count,
                ok_rate=round(ok_rate, 4),
                median_fetch=med_fetch,
                consecutive_failures=consec_fail,
                last_failure_ts=last_fail_ts,
                last_run_ts=last_run_ts if last_run_ts > 0 else None,
                in_quarantine=in_quarantine,
                awaiting_trial=awaiting_trial,
                cost=cost,
                p=round(p, 4),
                p_lower=round(p_lower, 4),
            )

        # 6. Обновляем кэш
        with self._lock:
            self._stats = new_stats
            self._pairs = new_pairs
            self._site_medians = new_site_medians
            self._site_overall_medians = new_site_overall_medians
            self._phone_multipliers = new_phone_multipliers
            self._phone_overall_multipliers = new_phone_overall_multipliers
            self._last_refresh = time.monotonic()

    def _ensure_fresh(self, now: float | None = None) -> None:
        refresh_sec = getattr(self.settings, "speed_refresh_seconds", 300) if self.settings else 300
        if (time.monotonic() - self._last_refresh) < refresh_sec:
            return
        # раскладка живого поиска идёт в потоках (pipeline → run_metrics_io): 20 поисков разом без
        # замка пересчитывали одну и ту же таблицу по 7 дням site_runs по нескольку раз параллельно
        with self._refresh_lock:
            if (time.monotonic() - self._last_refresh) >= refresh_sec:
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

            site_med = self.get_site_median(clean_site, clean_class)
            mult = (
                self._phone_multipliers.get((clean_dev, clean_class))
                or self._phone_overall_multipliers.get(clean_dev, 1.0)
            )
            est_c = round(site_med * mult, 2) if site_med is not None else None

            return SpeedStats(
                site=clean_site,
                phone=clean_dev,
                class_name=clean_class,
                n=0,
                ok_count=0,
                ok_rate=0.0,
                median_fetch=est_c,
                consecutive_failures=consec_fail,
                last_failure_ts=last_fail_ts,
                in_quarantine=in_quar,
                awaiting_trial=await_tr,
                cost=est_c,
                p=0.5,
                p_lower=0.0,
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
                return self._site_medians.get((clean_site, clean_class)) or self._site_overall_medians.get(clean_site)

            fetches: list[float] = []
            for (s, p, c), st in self._stats.items():
                if s == clean_site and c == clean_class and p != exclude_phone:
                    if st.median_fetch is not None:
                        fetches.append(st.median_fetch)
            if fetches:
                return round(statistics.median(fetches), 2)
            return self._site_medians.get((clean_site, clean_class)) or self._site_overall_medians.get(clean_site)

    def order_candidates(
        self,
        site: SiteConfig,
        base_candidates: Sequence[str],
        priority: int = 0,
        now: float | None = None,
    ) -> PreferenceList:
        """Сортирует список кандидатов по скорости и надёжности.

        - priority 0 (люди): порядок кандидатов по возрастанию C; пара допускается в «основные»,
          только если p_lower >= speed_live_min_p_lower (по умолчанию 0.6) и n >= speed_min_samples.
          Пары с меньшим p_lower остаются в конце списка как запасные, но не выбрасываются.
          Карантин/ожидание пробы — в самый конец.
        - priority > 0 (подписки/фон): новые пары (n == 0) и пробные попытки после карантина — первыми;
          затем пригодные по убыванию C (медленные первыми, быстрые в конце);
          быстрые берутся, если медленные заняты дольше speed_bg_patience_seconds;
          непригодные (карантин, p_lower < speed_live_min_p_lower при достаточном n) — в самый конец.
        """
        cands = [str(c).strip() for c in base_candidates if str(c).strip()]
        if not cands:
            return PreferenceList([])
        if len(cands) == 1:
            return PreferenceList(cands, patience=float(getattr(site, "phone_patience_seconds", 0) or 0))

        self._ensure_fresh(now=now)
        settings = self.settings
        min_samples = getattr(settings, "speed_min_samples", 5) if settings else 5
        min_p_lower = getattr(settings, "speed_live_min_p_lower", 0.6) if settings else 0.6
        bg_patience = getattr(settings, "speed_bg_patience_seconds", 60) if settings else 60
        site_name = getattr(site, "name", "")

        if priority == 0:
            # -------------------------------------------------------------
            # PRIORITY 0: ЛЮДИ (быстрые по C первыми)
            # -------------------------------------------------------------
            bad_candidates: list[str] = []
            primary_list: list[tuple[str, float | None, int]] = []
            backup_list: list[tuple[str, float | None, int]] = []

            for idx, dev in enumerate(cands):
                pair_st = self.get_pair_status(site_name, dev, now=now)
                st = self.get_stats(site_name, dev, "live", now=now)

                if pair_st.in_quarantine or pair_st.awaiting_trial:
                    bad_candidates.append(dev)
                elif st.n >= min_samples and st.p_lower >= min_p_lower:
                    primary_list.append((dev, st.cost, idx))
                else:
                    backup_list.append((dev, st.cost, idx))

            # Если у всех телефонов нет данных (n == 0), сохраняем исходный порядок (Req 2a)
            all_n_zero = all(
                self.get_stats(site_name, dev, "live", now=now).n == 0
                for dev in cands
            )
            if all_n_zero and not primary_list and not bad_candidates:
                orig_patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
                if isinstance(cands, PreferenceList):
                    return PreferenceList(cands, patience=orig_patience)
                return list(cands)

            primary_list.sort(key=lambda x: (0 if x[1] is not None else 1, x[1] if x[1] is not None else 0, x[2]))
            backup_list.sort(key=lambda x: (0 if x[1] is not None else 1, x[1] if x[1] is not None else 0, x[2]))

            primary_names = [x[0] for x in primary_list]
            backup_names = [x[0] for x in backup_list]

            ordered = primary_names + backup_names + bad_candidates

            if primary_names:
                preferred_count = len(primary_names)
                if backup_names:
                    primary_costs = [x[1] for x in primary_list if x[1] is not None]
                    backup_costs = [x[1] for x in backup_list if x[1] is not None]
                    if primary_costs and backup_costs:
                        min_primary = min(primary_costs)
                        min_backup = min(backup_costs)
                        patience = max(0.0, round(min_backup - min_primary, 1))
                    else:
                        patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
                else:
                    patience = float(getattr(site, "phone_patience_seconds", 0) or 0)
            else:
                preferred_count = 1
                patience = float(getattr(site, "phone_patience_seconds", 0) or 0)

            return PreferenceList(ordered, patience=patience, preferred_count=preferred_count)

        else:
            # -------------------------------------------------------------
            # PRIORITY > 0: ПОДПИСКИ И ФОН (медленные первыми, исследование новых)
            # -------------------------------------------------------------
            bad_candidates: list[str] = []
            trial_candidates: list[str] = []
            exploratory_candidates: list[str] = []
            eligible: list[tuple[str, float | None, int, int]] = []

            for idx, dev in enumerate(cands):
                pair_st = self.get_pair_status(site_name, dev, now=now)
                st = self.get_stats(site_name, dev, "bg", now=now)
                live_st = self.get_stats(site_name, dev, "live", now=now)

                is_unreliable = (st.n >= min_samples and st.p_lower < min_p_lower) or (
                    st.n < min_samples and live_st.n >= min_samples and live_st.p_lower < min_p_lower
                )

                if pair_st.in_quarantine or is_unreliable:
                    bad_candidates.append(dev)
                elif pair_st.awaiting_trial:
                    trial_candidates.append(dev)
                elif (st.n + live_st.n == 0) and pair_st.total_runs == 0:
                    exploratory_candidates.append(dev)
                else:
                    if st.n >= min_samples and st.cost is not None:
                        est = st.cost
                    elif live_st.n >= min_samples and live_st.cost is not None:
                        est = live_st.cost
                    else:
                        est = st.cost or live_st.cost
                    eligible.append((dev, est, st.n, idx))

            eligible.sort(key=lambda x: (0 if x[1] is not None else 1, -(x[1] or 0), x[3]))

            measured_ests = [x[1] for x in eligible if x[1] is not None]
            if len(measured_ests) >= 2:
                fastest_est = min(measured_ests)
                slowest_est = max(measured_ests)
                if slowest_est > fastest_est:
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

            lead_names = trial_candidates + exploratory_candidates
            ordered = lead_names + slow_names + fast_names + bad_candidates

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
        path = db_path(settings.storage_dir, "metrics")
    else:
        path = db_path(Path("state"), "metrics")

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
