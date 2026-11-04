"""История цен в SQLite: запоминает цену каждого билета, строит сводки и аналитику."""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Iterable
import uuid

from .models import Offer, Query, SiteResult
from .offers import baggage_state

# Если цена билета не изменилась и последняя запись свежее этого срока, новую строку не пишем
SAME_PRICE_TTL = 600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id          TEXT NOT NULL,
    site              TEXT NOT NULL,
    origin            TEXT NOT NULL,
    destination       TEXT NOT NULL,
    depart            TEXT NOT NULL,
    return_date       TEXT,
    adults            INTEGER NOT NULL,
    price             INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    observed_at       REAL NOT NULL,
    days_to_departure INTEGER,
    baggage           TEXT,
    airline           TEXT,
    flight_numbers    TEXT,
    depart_at         TEXT,
    search_id         TEXT
);
CREATE INDEX IF NOT EXISTS ix_price_offer ON price_history (offer_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_price_route ON price_history (origin, destination, depart, return_date);
CREATE INDEX IF NOT EXISTS ix_price_observed_at ON price_history (observed_at);

CREATE TABLE IF NOT EXISTS route_daily (
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    depart       TEXT NOT NULL,
    return_date  TEXT NOT NULL DEFAULT '',
    adults       INTEGER NOT NULL,
    day          TEXT NOT NULL,
    min_price    INTEGER NOT NULL,
    p25_price    REAL NOT NULL,
    median_price REAL NOT NULL,
    samples      INTEGER NOT NULL,
    PRIMARY KEY (origin, destination, depart, return_date, adults, day)
);
CREATE INDEX IF NOT EXISTS ix_route_daily_route ON route_daily (origin, destination, depart, return_date);

CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_heartbeat_ts ON scheduler_heartbeat (ts);
"""


def _normalize_date(val: date | datetime | str | None) -> date | None:
    """Приводит date, datetime или ISO-строку к объекту date."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        if "T" in val_str or " " in val_str:
            return datetime.fromisoformat(val_str).date()
        return date.fromisoformat(val_str)
    return date.fromisoformat(str(val))


def _model_day_prices(min_p: float, p25_p: float, med_p: float, count: int) -> list[int]:
    """Моделирует распределение цен за день по min, p25, median и количеству замеров."""
    if count <= 0:
        return []
    if count == 1:
        return [int(round(min_p))]
    if count == 2:
        p2 = max(min_p, 2.0 * med_p - min_p)
        return [int(round(min_p)), int(round(p2))]

    iqr = max(0.0, med_p - p25_p)
    p75_est = med_p + iqr
    spread = max(0.0, med_p - min_p)
    max_est = max(p75_est, med_p + spread)

    prices: list[int] = []
    for i in range(count):
        frac = i / (count - 1)
        if frac <= 0.25:
            t = frac / 0.25
            val = min_p + t * (p25_p - min_p)
        elif frac <= 0.5:
            t = (frac - 0.25) / 0.25
            val = p25_p + t * (med_p - p25_p)
        elif frac <= 0.75:
            t = (frac - 0.5) / 0.25
            val = med_p + t * (p75_est - med_p)
        else:
            t = (frac - 0.75) / 0.25
            val = p75_est + t * (max_est - p75_est)
        prices.append(int(round(val)))
    return prices


class PriceHistory:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _migrate(self, db: sqlite3.Connection) -> None:
        """Идемпотентно добавляет недостающие колонки и индексы к price_history."""
        info = db.execute("PRAGMA table_info(price_history)").fetchall()
        existing_cols = {col[1] for col in info}
        expected_cols = [
            ("days_to_departure", "INTEGER"),
            ("baggage", "TEXT"),
            ("airline", "TEXT"),
            ("flight_numbers", "TEXT"),
            ("depart_at", "TEXT"),
            ("search_id", "TEXT"),
        ]
        for col_name, col_type in expected_cols:
            if col_name not in existing_cols:
                db.execute(f"ALTER TABLE price_history ADD COLUMN {col_name} {col_type}")
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_price_route_obs "
            "ON price_history (origin, destination, depart, return_date, observed_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_price_search_id "
            "ON price_history (search_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ix_price_observed_at "
            "ON price_history (observed_at)"
        )

    def annotate(
        self,
        q: Query,
        results: Iterable[SiteResult],
        now: float | None = None,
        search_id: str | None = None,
    ) -> None:
        """Проставляет prev_price/prev_at/min_price у билетов и записывает текущие цены."""
        now = time.time() if now is None else float(now)
        search_id = search_id or uuid.uuid4().hex[:12]
        offers = [o for r in results if r.ok for o in r.offers if o.offer_id]
        if not offers:
            return

        depart_date = _normalize_date(q.depart)
        if depart_date is None:
            return
        obs_date = datetime.fromtimestamp(now).date()
        days_to_dep = (depart_date - obs_date).days
        ret_date = _normalize_date(q.return_date)
        ret = ret_date.isoformat() if ret_date else None

        with self._connect() as db:
            for o in offers:
                last = db.execute(
                    "SELECT price, observed_at FROM price_history "
                    "WHERE offer_id = ? AND currency = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
                    (o.offer_id, o.currency),
                ).fetchone()
                low = db.execute(
                    "SELECT MIN(price) FROM price_history WHERE offer_id = ? AND currency = ?",
                    (o.offer_id, o.currency),
                ).fetchone()
                if last is not None:
                    o.prev_price, o.prev_at = int(last[0]), float(last[1])
                    o.min_price = int(low[0]) if low and low[0] is not None else None
                fresh_same = last is not None and int(last[0]) == o.price and now - float(last[1]) < SAME_PRICE_TTL
                if fresh_same:
                    continue

                b_state = baggage_state(o)
                baggage = "with" if b_state is True else ("without" if b_state is False else None)
                airline = o.airline
                if isinstance(o.flight_numbers, list):
                    flight_numbers = ",".join(str(fn) for fn in o.flight_numbers) if o.flight_numbers else None
                elif isinstance(o.flight_numbers, str):
                    flight_numbers = o.flight_numbers or None
                else:
                    flight_numbers = None
                depart_at = o.depart_at or o.depart_time or None

                db.execute(
                    "INSERT INTO price_history "
                    "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, "
                    "days_to_departure, baggage, airline, flight_numbers, depart_at, search_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        o.offer_id, o.site, q.origin, q.destination, depart_date.isoformat(),
                        ret, q.adults, o.price, o.currency, now,
                        days_to_dep, baggage, airline, flight_numbers, depart_at, search_id,
                    ),
                )

    def rebuild_route_daily(self, day: date | None = None) -> int:
        """Пересчитывает сводки route_daily из сырых записей за указанный день (или за вчера и сегодня)."""
        d_norm = _normalize_date(day) if day is not None else None
        if d_norm is not None:
            target_days = [d_norm]
        else:
            today = date.today()
            target_days = [today - timedelta(days=1), today]

        total_written = 0
        with self._connect() as db:
            for d in target_days:
                day_str = d.isoformat()
                start_ts = datetime.combine(d, dt_time.min).timestamp()
                end_ts = datetime.combine(d + timedelta(days=1), dt_time.min).timestamp()
                rows = db.execute(
                    "SELECT origin, destination, depart, COALESCE(return_date, ''), adults, "
                    "price, search_id, observed_at "
                    "FROM price_history "
                    "WHERE observed_at >= ? AND observed_at < ?",
                    (start_ts, end_ts),
                ).fetchall()
                if not rows:
                    continue

                routes: dict[tuple, dict[str, list[int]]] = {}
                for origin, destination, depart, ret, adults, price, s_id, obs_at in rows:
                    route_key = (origin, destination, depart, ret, adults)
                    if route_key not in routes:
                        routes[route_key] = {}
                    sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
                    if sample_key not in routes[route_key]:
                        routes[route_key][sample_key] = []
                    routes[route_key][sample_key].append(int(price))

                for (origin, destination, depart, ret, adults), samples_dict in routes.items():
                    best_prices = [min(prices) for prices in samples_dict.values()]
                    if not best_prices:
                        continue
                    samples_count = len(best_prices)
                    min_p = min(best_prices)
                    med_p = float(statistics.median(best_prices))
                    if len(best_prices) >= 2:
                        p25_p = float(statistics.quantiles(best_prices, n=4)[0])
                    else:
                        p25_p = float(best_prices[0])

                    db.execute(
                        "INSERT INTO route_daily "
                        "(origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(origin, destination, depart, return_date, adults, day) DO UPDATE SET "
                        "min_price = excluded.min_price, "
                        "p25_price = excluded.p25_price, "
                        "median_price = excluded.median_price, "
                        "samples = excluded.samples",
                        (origin, destination, depart, ret, adults, day_str, min_p, p25_p, med_p, samples_count),
                    )
                    total_written += 1

        return total_written

    def prune(self, keep_days: int = 45, now: float | None = None) -> int:
        """Сворачивает старые дни в route_daily и удаляет сырые записи старше keep_days."""
        now = time.time() if now is None else float(now)
        # Выравниваем границу чистки по началу календарного дня, чтобы дни не удалялись частично
        cutoff_day = datetime.fromtimestamp(now).date() - timedelta(days=keep_days)
        cutoff_ts = datetime.combine(cutoff_day, dt_time.min).timestamp()

        with self._connect() as db:
            min_row = db.execute(
                "SELECT MIN(observed_at) FROM price_history WHERE observed_at < ?",
                (cutoff_ts,),
            ).fetchone()
            if not min_row or min_row[0] is None:
                return 0
            min_day = datetime.fromtimestamp(min_row[0]).date()

            # Пересчёт идемпотентен, поэтому сворачиваем каждый день с сырыми записями:
            # так поздние замеры уже частично свёрнутого дня не пропадут при удалении.
            cur_day = min_day
            days_to_rebuild = []
            while cur_day < cutoff_day:
                day_start = datetime.combine(cur_day, dt_time.min).timestamp()
                day_end = datetime.combine(cur_day + timedelta(days=1), dt_time.min).timestamp()
                has_records = db.execute(
                    "SELECT 1 FROM price_history WHERE observed_at >= ? AND observed_at < ? LIMIT 1",
                    (day_start, day_end),
                ).fetchone()
                if has_records:
                    days_to_rebuild.append(cur_day)
                cur_day += timedelta(days=1)

        for d in days_to_rebuild:
            self.rebuild_route_daily(d)

        with self._connect() as db:
            cursor = db.execute("DELETE FROM price_history WHERE observed_at < ?", (cutoff_ts,))
            return cursor.rowcount

    def record_heartbeat(self, now: float | None = None) -> None:
        """Записывает отметку времени работы планировщика (хранит не более 5000 последних)."""
        ts = time.time() if now is None else float(now)
        with self._connect() as db:
            db.execute("INSERT INTO scheduler_heartbeat (ts) VALUES (?)", (ts,))
            db.execute(
                "DELETE FROM scheduler_heartbeat WHERE rowid NOT IN "
                "(SELECT rowid FROM scheduler_heartbeat ORDER BY ts DESC, rowid DESC LIMIT 5000)"
            )

    def coverage(self, since: float, until: float, gap_seconds: float = 3600.0) -> dict:
        """Возвращает информацию о покрытии: covered_seconds и список интервалов-пропусков gaps."""
        if until <= since:
            return {"covered_seconds": 0.0, "gaps": []}

        with self._connect() as db:
            prev_row = db.execute(
                "SELECT ts FROM scheduler_heartbeat WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                (since,),
            ).fetchone()
            mid_rows = db.execute(
                "SELECT ts FROM scheduler_heartbeat WHERE ts > ? AND ts < ? ORDER BY ts ASC",
                (since, until),
            ).fetchall()
            next_row = db.execute(
                "SELECT ts FROM scheduler_heartbeat WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
                (until,),
            ).fetchone()

        raw_points: list[float] = []
        if prev_row:
            raw_points.append(float(prev_row[0]))
        for r in mid_rows:
            raw_points.append(float(r[0]))
        if next_row:
            raw_points.append(float(next_row[0]))

        if not raw_points:
            return {
                "covered_seconds": 0.0,
                "gaps": [(float(since), float(until))],
            }

        # До until не было ни одной метки: метка из будущего не доказывает работу в прошлом
        if not prev_row and not mid_rows:
            return {"covered_seconds": 0.0, "gaps": [(float(since), float(until))]}

        raw_gaps: list[tuple[float, float]] = []
        first_pt = raw_points[0]
        if first_pt > since and (first_pt - since) > gap_seconds:
            raw_gaps.append((float(since), min(float(until), float(first_pt))))

        for p1, p2 in zip(raw_points, raw_points[1:]):
            if p2 - p1 > gap_seconds:
                g_start = max(float(since), float(p1))
                g_end = min(float(until), float(p2))
                if g_start < g_end:
                    raw_gaps.append((g_start, g_end))

        last_pt = raw_points[-1]
        if last_pt < until and (until - last_pt) > gap_seconds:
            g_start = max(float(since), float(last_pt))
            raw_gaps.append((g_start, float(until)))

        # Объединение перекрывающихся или соприкасающихся интервалов
        merged_gaps: list[tuple[float, float]] = []
        for start, end in raw_gaps:
            if not merged_gaps:
                merged_gaps.append((start, end))
            else:
                last_s, last_e = merged_gaps[-1]
                if start <= last_e:
                    merged_gaps[-1] = (last_s, max(last_e, end))
                else:
                    merged_gaps.append((start, end))

        gap_total = sum(end - start for start, end in merged_gaps)
        covered = max(0.0, (until - since) - gap_total)
        return {
            "covered_seconds": round(covered, 3),
            "gaps": merged_gaps,
        }

    def route_stats(self, q: Query) -> dict | None:
        """Возвращает аналитику по маршруту: samples, min, median, p25, last_price, percentile_of_last, trends, volatility."""
        depart_d = _normalize_date(q.depart)
        depart_str = depart_d.isoformat() if depart_d else str(q.depart)
        ret_d = _normalize_date(q.return_date)
        ret_str = ret_d.isoformat() if ret_d else None
        ret_daily = ret_str or ""

        with self._connect() as db:
            if ret_str is None:
                cond_ret = "return_date IS NULL"
                raw_rows = db.execute(
                    "SELECT price, search_id, observed_at FROM price_history "
                    f"WHERE origin = ? AND destination = ? AND depart = ? AND {cond_ret} AND adults = ? "
                    "ORDER BY observed_at ASC",
                    (q.origin, q.destination, depart_str, q.adults),
                ).fetchall()
            else:
                cond_ret = "return_date = ?"
                raw_rows = db.execute(
                    "SELECT price, search_id, observed_at FROM price_history "
                    f"WHERE origin = ? AND destination = ? AND depart = ? AND {cond_ret} AND adults = ? "
                    "ORDER BY observed_at ASC",
                    (q.origin, q.destination, depart_str, ret_str, q.adults),
                ).fetchall()

            daily_rows = db.execute(
                "SELECT day, min_price, p25_price, median_price, samples FROM route_daily "
                "WHERE origin = ? AND destination = ? AND depart = ? AND return_date = ? AND adults = ? "
                "ORDER BY day ASC",
                (q.origin, q.destination, depart_str, ret_daily, q.adults),
            ).fetchall()

        # Группируем сырые строки по замерам поиска
        raw_searches_dict: dict[str, dict] = {}
        for price, s_id, obs_at in raw_rows:
            sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
            if sample_key not in raw_searches_dict:
                raw_searches_dict[sample_key] = {"prices": [], "ts": obs_at}
            raw_searches_dict[sample_key]["prices"].append(int(price))
            raw_searches_dict[sample_key]["ts"] = min(raw_searches_dict[sample_key]["ts"], obs_at)

        raw_samples: list[tuple[float, int, str]] = []
        for s in raw_searches_dict.values():
            best_p = min(s["prices"])
            ts = float(s["ts"])
            day_s = datetime.fromtimestamp(ts).date().isoformat()
            raw_samples.append((ts, best_p, day_s))
        raw_samples.sort(key=lambda x: x[0])

        raw_days = {s[2] for s in raw_samples}

        # Добавляем дни из route_daily, которых нет в сырых записях (свёрнутые)
        older_samples: list[tuple[float, int]] = []
        for day_val, min_p, p25_p, med_p, count in daily_rows:
            if day_val not in raw_days:
                d_obj = date.fromisoformat(day_val)
                day_ts = datetime.combine(d_obj, dt_time(12, 0)).timestamp()
                day_prices = _model_day_prices(float(min_p), float(p25_p), float(med_p), int(count))
                for p in day_prices:
                    older_samples.append((day_ts, p))

        combined: list[tuple[float, int]] = older_samples + [(s[0], s[1]) for s in raw_samples]
        combined.sort(key=lambda x: x[0])

        total_samples = len(combined)
        if total_samples < 3:
            return None

        prices = [p for _, p in combined]
        last_price = raw_samples[-1][1] if raw_samples else combined[-1][1]
        min_price = min(prices)
        med_price = float(statistics.median(prices))
        if len(prices) >= 2:
            p25_price = float(statistics.quantiles(prices, n=4)[0])
        else:
            p25_price = float(prices[0])

        expensive_count = sum(1 for p in prices if p > last_price)
        percentile_of_last = round((expensive_count / len(prices)) * 100.0, 1)

        trend_3d = self._trend(combined, days=3)
        trend_7d = self._trend(combined, days=7)

        mean_price = statistics.mean(prices)
        stdev_price = statistics.stdev(prices) if len(prices) >= 2 else 0.0
        volatility_pct = round((stdev_price / mean_price) * 100.0, 1) if mean_price > 0 else None

        return {
            "samples": total_samples,
            "min": min_price,
            "median": med_price,
            "p25": p25_price,
            "last_price": last_price,
            "percentile_of_last": percentile_of_last,
            "trend_3d": trend_3d,
            "trend_7d": trend_7d,
            "volatility_pct": volatility_pct,
        }

    @staticmethod
    def _trend(samples_with_ts: list[tuple[float, int]], days: int) -> str | None:
        if not samples_with_ts:
            return None
        first_ts = samples_with_ts[0][0]
        last_ts, last_price = samples_with_ts[-1]
        # Проверяем, что временной охват достаточен для оценки тренда
        if (last_ts - first_ts) < days * 0.8 * 86400.0:
            return None

        target_ts = last_ts - days * 86400.0
        past = [s for s in samples_with_ts if s[0] < last_ts]
        if not past:
            return None
        best = min(past, key=lambda s: abs(s[0] - target_ts))
        max_dist = min(0.8 * 86400.0, days * 0.25 * 86400.0)
        if abs(best[0] - target_ts) > max_dist:
            return None
        old_price = best[1]
        if old_price <= 0:
            return None
        diff_pct = (last_price - old_price) / float(old_price)
        if diff_pct > 0.02:
            return "up"
        elif diff_pct < -0.02:
            return "down"
        else:
            return "flat"

    def route_series(
        self,
        q: Query,
        days: int = 30,
        now: float | None = None,
    ) -> list[tuple[float, int]]:
        """Точки (timestamp, лучшая цена замера) для графика маршрута q за указанное число дней."""
        if days <= 0:
            return []
        now_ts = time.time() if now is None else float(now)
        since_ts = now_ts - days * 86400.0

        depart_d = _normalize_date(q.depart)
        depart_str = depart_d.isoformat() if depart_d else str(q.depart)
        ret_d = _normalize_date(q.return_date)
        ret_str = ret_d.isoformat() if ret_d else None
        ret_daily = ret_str or ""

        with self._connect() as db:
            if ret_str is None:
                cond_ret = "return_date IS NULL"
                raw_rows = db.execute(
                    "SELECT price, search_id, observed_at FROM price_history "
                    f"WHERE origin = ? AND destination = ? AND depart = ? AND {cond_ret} AND adults = ? "
                    "AND observed_at >= ? AND observed_at <= ? "
                    "ORDER BY observed_at ASC",
                    (q.origin, q.destination, depart_str, q.adults, since_ts, now_ts),
                ).fetchall()
            else:
                cond_ret = "return_date = ?"
                raw_rows = db.execute(
                    "SELECT price, search_id, observed_at FROM price_history "
                    f"WHERE origin = ? AND destination = ? AND depart = ? AND {cond_ret} AND adults = ? "
                    "AND observed_at >= ? AND observed_at <= ? "
                    "ORDER BY observed_at ASC",
                    (q.origin, q.destination, depart_str, ret_str, q.adults, since_ts, now_ts),
                ).fetchall()

            daily_rows = db.execute(
                "SELECT day, median_price FROM route_daily "
                "WHERE origin = ? AND destination = ? AND depart = ? AND return_date = ? AND adults = ? "
                "ORDER BY day ASC",
                (q.origin, q.destination, depart_str, ret_daily, q.adults),
            ).fetchall()

        raw_searches_dict: dict[str, dict] = {}
        for price, s_id, obs_at in raw_rows:
            sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
            if sample_key not in raw_searches_dict:
                raw_searches_dict[sample_key] = {"prices": [], "ts": obs_at}
            raw_searches_dict[sample_key]["prices"].append(int(price))
            raw_searches_dict[sample_key]["ts"] = min(raw_searches_dict[sample_key]["ts"], obs_at)

        raw_samples: list[tuple[float, int, str]] = []
        for s in raw_searches_dict.values():
            best_p = min(s["prices"])
            ts = float(s["ts"])
            day_s = datetime.fromtimestamp(ts).date().isoformat()
            raw_samples.append((ts, best_p, day_s))

        raw_days = {s[2] for s in raw_samples}

        older_samples: list[tuple[float, int]] = []
        for day_val, med_p in daily_rows:
            d_obj = date.fromisoformat(day_val)
            day_ts = datetime.combine(d_obj, dt_time(12, 0)).timestamp()
            if day_ts < since_ts or day_ts > now_ts:
                continue
            if day_val not in raw_days:
                older_samples.append((day_ts, int(round(float(med_p)))))

        combined: list[tuple[float, int]] = older_samples + [(s[0], s[1]) for s in raw_samples]
        combined.sort(key=lambda x: x[0])
        return combined

    def route_search_minima(
        self,
        q: Query,
        since_ts: float,
        until_ts: float | None = None,
    ) -> list[int]:
        """Список лучших цен каждого замера для маршрута q за указанный период времени."""
        depart_d = _normalize_date(q.depart)
        depart_str = depart_d.isoformat() if depart_d else str(q.depart)
        ret_d = _normalize_date(q.return_date)
        ret_str = ret_d.isoformat() if ret_d else None

        since_val = float(since_ts)
        until_val = float(until_ts) if until_ts is not None else None

        cond_ret = "return_date IS NULL" if ret_str is None else "return_date = ?"
        sql = (
            "SELECT price, search_id, observed_at FROM price_history "
            f"WHERE origin = ? AND destination = ? AND depart = ? AND {cond_ret} AND adults = ? "
            "AND observed_at >= ? "
        )
        params: list[object] = [q.origin, q.destination, depart_str]
        if ret_str is not None:
            params.append(ret_str)
        params.extend([q.adults, since_val])

        if until_val is not None:
            sql += "AND observed_at <= ? "
            params.append(until_val)

        sql += "ORDER BY observed_at ASC"

        with self._connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()

        searches: dict[str, dict] = {}
        for price, s_id, obs_at in rows:
            sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
            if sample_key not in searches:
                searches[sample_key] = {"prices": [], "ts": obs_at}
            searches[sample_key]["prices"].append(int(price))
            searches[sample_key]["ts"] = min(searches[sample_key]["ts"], obs_at)

        ordered = sorted(searches.values(), key=lambda s: s["ts"])
        return [min(s["prices"]) for s in ordered]

    def cheapest_site_stats(
        self,
        q: Query,
        days: int = 30,
        now: float | None = None,
    ) -> list[tuple[str, int, int]]:
        """По сайтам маршрута q: (site, число побед, число участий), сортировка по числу побед по убыванию."""
        if days <= 0:
            return []
        now_ts = time.time() if now is None else float(now)
        since_ts = now_ts - days * 86400.0

        depart_d = _normalize_date(q.depart)
        depart_str = depart_d.isoformat() if depart_d else str(q.depart)
        ret_d = _normalize_date(q.return_date)
        ret_str = ret_d.isoformat() if ret_d else None

        cond_ret = "return_date IS NULL" if ret_str is None else "return_date = ?"
        sql = (
            "SELECT site, price, search_id, observed_at FROM price_history "
            f"WHERE origin = ? AND destination = ? AND depart = ? AND {cond_ret} AND adults = ? "
            "AND observed_at >= ? AND observed_at <= ? "
            "ORDER BY observed_at ASC"
        )
        params: list[object] = [q.origin, q.destination, depart_str]
        if ret_str is not None:
            params.append(ret_str)
        params.extend([q.adults, since_ts, now_ts])

        with self._connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()

        searches: dict[str, dict[str, list[int]]] = {}
        for site, price, s_id, obs_at in rows:
            sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
            if sample_key not in searches:
                searches[sample_key] = {}
            if site not in searches[sample_key]:
                searches[sample_key][site] = []
            searches[sample_key][site].append(int(price))

        wins: dict[str, int] = {}
        participated: dict[str, int] = {}

        for sample_key, site_prices in searches.items():
            all_prices = [p for prices in site_prices.values() for p in prices]
            if not all_prices:
                continue
            best_p = min(all_prices)

            for site, prices in site_prices.items():
                participated[site] = participated.get(site, 0) + 1
                if min(prices) == best_p:
                    wins[site] = wins.get(site, 0) + 1

        stats = [
            (site, wins.get(site, 0), participated[site])
            for site in participated
        ]
        stats.sort(key=lambda x: (-x[1], -x[2], x[0]))
        return stats

    def popular_routes(
        self,
        limit: int = 10,
        days: int = 7,
        now: float | None = None,
    ) -> list[tuple[str, str, int]]:
        """Популярные направления за days дней: (origin, destination, число поисков) по убыванию."""
        if limit <= 0 or days <= 0:
            return []
        now_ts = time.time() if now is None else float(now)
        since_ts = now_ts - days * 86400.0

        with self._connect() as db:
            rows = db.execute(
                "SELECT origin, destination, search_id, observed_at FROM price_history "
                "WHERE observed_at >= ? AND observed_at <= ?",
                (since_ts, now_ts),
            ).fetchall()

        route_samples: dict[tuple[str, str], set[str]] = {}
        for origin, destination, s_id, obs_at in rows:
            sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
            key = (origin, destination)
            if key not in route_samples:
                route_samples[key] = set()
            route_samples[key].add(sample_key)

        result = [
            (orig, dest, len(samples))
            for (orig, dest), samples in route_samples.items()
        ]
        result.sort(key=lambda x: (-x[2], x[0], x[1]))
        return result[:limit]

    def price_curve(
        self,
        days: int = 180,
        now: float | None = None,
        min_samples: int = 30,
    ) -> dict[str, dict]:
        """Кривая цены от числа дней до вылета по всем маршрутам сразу.

        Корзины days_to_departure: «0-7», «8-14», «15-21», «22-30», «31-60», «61+».
        Для каждого замера вычисляется отношение лучшей цены замера к медиане маршрута за тот же период,
        затем по каждой корзине берётся медиана отношений. Корзины с samples < min_samples не включаются.
        """
        if days <= 0:
            return {}
        now_ts = time.time() if now is None else float(now)
        since_ts = now_ts - days * 86400.0

        with self._connect() as db:
            rows = db.execute(
                "SELECT origin, destination, depart, COALESCE(return_date, ''), adults, "
                "price, search_id, observed_at, days_to_departure "
                "FROM price_history "
                "WHERE observed_at >= ? AND observed_at <= ? "
                "ORDER BY observed_at ASC",
                (since_ts, now_ts),
            ).fetchall()

        if not rows:
            return {}

        routes: dict[tuple, dict[str, dict]] = {}
        for origin, destination, depart, ret, adults, price, s_id, obs_at, days_dep in rows:
            route_key = (origin, destination, depart, ret, adults)
            if route_key not in routes:
                routes[route_key] = {}
            sample_key = str(s_id) if s_id else f"m_{round(obs_at / 60.0)}"
            if sample_key not in routes[route_key]:
                if days_dep is not None:
                    d_val = int(days_dep)
                else:
                    dep_d = _normalize_date(depart)
                    obs_d = datetime.fromtimestamp(obs_at).date()
                    d_val = (dep_d - obs_d).days if dep_d is not None else None
                routes[route_key][sample_key] = {
                    "prices": [],
                    "days_to_departure": d_val,
                }
            elif routes[route_key][sample_key]["days_to_departure"] is None and days_dep is not None:
                routes[route_key][sample_key]["days_to_departure"] = int(days_dep)

            routes[route_key][sample_key]["prices"].append(int(price))

        bucket_order = ["0-7", "8-14", "15-21", "22-30", "31-60", "61+"]
        bucket_ratios: dict[str, list[float]] = {b: [] for b in bucket_order}

        for searches in routes.values():
            sample_best_prices: list[int] = []
            for s_data in searches.values():
                if s_data["prices"]:
                    best_p = min(s_data["prices"])
                    s_data["best_price"] = best_p
                    sample_best_prices.append(best_p)

            if not sample_best_prices:
                continue

            route_median = float(statistics.median(sample_best_prices))
            if route_median <= 0:
                continue

            for s_data in searches.values():
                best_p = s_data.get("best_price")
                d = s_data.get("days_to_departure")
                if best_p is None or d is None or d < 0:
                    continue

                ratio = best_p / route_median

                b_name = None
                if 0 <= d <= 7:
                    b_name = "0-7"
                elif 8 <= d <= 14:
                    b_name = "8-14"
                elif 15 <= d <= 21:
                    b_name = "15-21"
                elif 22 <= d <= 30:
                    b_name = "22-30"
                elif 31 <= d <= 60:
                    b_name = "31-60"
                elif d >= 61:
                    b_name = "61+"

                if b_name is not None:
                    bucket_ratios[b_name].append(ratio)

        result: dict[str, dict] = {}
        for b in bucket_order:
            ratios = bucket_ratios[b]
            if len(ratios) >= max(1, min_samples):
                result[b] = {
                    "ratio": round(float(statistics.median(ratios)), 4),
                    "samples": len(ratios),
                }
        return result

