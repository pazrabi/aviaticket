"""Хранилище метрик парсинга авиабилетов в SQLite."""
from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import time
from typing import Any, Sequence

from .models import SiteResult

log = logging.getLogger("avia.metrics")

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    source        TEXT NOT NULL,
    site          TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    partial       INTEGER NOT NULL,
    captcha       INTEGER NOT NULL,
    error         TEXT,
    offers        INTEGER NOT NULL,
    elapsed       REAL NOT NULL,
    device        TEXT,
    queue_wait    REAL,
    fetch_seconds REAL,
    priority      INTEGER,
    min_price     REAL
);
CREATE INDEX IF NOT EXISTS ix_site_runs_ts ON site_runs (ts);
CREATE INDEX IF NOT EXISTS ix_site_runs_site_ts ON site_runs (site, ts);

CREATE TABLE IF NOT EXISTS attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    search_id     TEXT,
    site          TEXT NOT NULL,
    device        TEXT,
    source        TEXT NOT NULL,
    role          TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    offers        INTEGER NOT NULL,
    fetch_seconds REAL,
    queue_wait    REAL
);
CREATE INDEX IF NOT EXISTS ix_attempts_ts ON attempts (ts);
CREATE INDEX IF NOT EXISTS ix_attempts_search_id ON attempts (search_id);
CREATE INDEX IF NOT EXISTS ix_attempts_site_ts ON attempts (site, ts);

CREATE TABLE IF NOT EXISTS searches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    source         TEXT NOT NULL,
    total_seconds  REAL NOT NULL,
    first_seconds  REAL,
    sites_total    INTEGER NOT NULL,
    sites_ok       INTEGER NOT NULL,
    queue_seconds  REAL
);
CREATE INDEX IF NOT EXISTS ix_searches_ts ON searches (ts);

CREATE TABLE IF NOT EXISTS parser_watch_state (
    site              TEXT PRIMARY KEY,
    last_status       TEXT NOT NULL,
    last_alert_ts     REAL NOT NULL,
    last_alert_status TEXT NOT NULL
);
"""


class SiteOutcome(tuple):
    """Кортеж (device, ok) с дополнительными атрибутами captcha и ts."""

    def __new__(cls, device: str | None, ok: bool, captcha: bool = False, ts: float = 0.0) -> SiteOutcome:
        return super().__new__(cls, (device, ok))

    @property
    def device(self) -> str | None:
        return self[0]

    @property
    def ok(self) -> bool:
        return self[1]

    @property
    def captcha(self) -> bool:
        return bool(self._captcha)

    @property
    def ts(self) -> float:
        return float(self._ts)

    def __init__(self, device: str | None, ok: bool, captcha: bool = False, ts: float = 0.0) -> None:
        self._captcha = captcha
        self._ts = ts


class MetricsStore:
    """Хранилище метрик работы поисковых модулей и сайтов."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._initialized = False

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as conn:
            conn.executescript(SCHEMA)
            # Миграция: проверяем и добавляем недостающие колонки (идемпотентно)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(site_runs)").fetchall()}
            if "device" not in cols:
                conn.execute("ALTER TABLE site_runs ADD COLUMN device TEXT")
            if "queue_wait" not in cols:
                conn.execute("ALTER TABLE site_runs ADD COLUMN queue_wait REAL")
            if "fetch_seconds" not in cols:
                conn.execute("ALTER TABLE site_runs ADD COLUMN fetch_seconds REAL")
            if "priority" not in cols:
                conn.execute("ALTER TABLE site_runs ADD COLUMN priority INTEGER")
            if "min_price" not in cols:
                conn.execute("ALTER TABLE site_runs ADD COLUMN min_price REAL")
        self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self._ensure_schema()
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record_run(
        self,
        source: str,
        site: str,
        ok: bool | int,
        partial: bool | int,
        captcha: bool | int,
        error: str | None,
        offers: int,
        elapsed: float,
        ts: float | None = None,
        device: str | None = None,
        queue_wait: float | None = None,
        fetch_seconds: float | None = None,
        priority: int | None = None,
        min_price: float | int | None = None,
    ) -> None:
        """Записывает результат отдельного запуска сайта."""
        try:
            record_ts = time.time() if ts is None else float(ts)
            err_str = str(error)[:300] if error else None
            dev_str = str(device).strip() if device and str(device).strip() else None
            qw_val = float(queue_wait) if queue_wait is not None else None
            fs_val = float(fetch_seconds) if fetch_seconds is not None else None
            prio_val = int(priority) if priority is not None else None
            mp_val = float(min_price) if min_price is not None else None
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO site_runs (
                        ts, source, site, ok, partial, captcha, error, offers, elapsed,
                        device, queue_wait, fetch_seconds, priority, min_price
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_ts,
                        source,
                        site,
                        1 if ok else 0,
                        1 if partial else 0,
                        1 if captcha else 0,
                        err_str,
                        int(offers),
                        float(elapsed),
                        dev_str,
                        qw_val,
                        fs_val,
                        prio_val,
                        mp_val,
                    ),
                )
        except Exception:
            log.exception("ошибка записи метрики site_run")

    def record_attempt(
        self,
        site: str,
        device: str | None = None,
        source: str = "user",
        role: str = "primary",
        outcome: str = "ok",
        offers: int = 0,
        fetch_seconds: float | None = None,
        queue_wait: float | None = None,
        search_id: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Записывает попытку обращения к сайту на конкретном телефоне."""
        try:
            record_ts = time.time() if ts is None else float(ts)
            s_id = str(search_id).strip() if search_id and str(search_id).strip() else None
            dev_str = str(device).strip() if device and str(device).strip() else None
            src_str = str(source).strip() if source else "user"
            role_str = str(role).strip() if role else "primary"
            outcome_str = str(outcome).strip() if outcome else "ok"
            offers_cnt = int(offers) if offers is not None else 0
            fs_val = float(fetch_seconds) if fetch_seconds is not None else None
            qw_val = float(queue_wait) if queue_wait is not None else None

            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO attempts (
                        ts, search_id, site, device, source, role, outcome, offers, fetch_seconds, queue_wait
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_ts,
                        s_id,
                        site,
                        dev_str,
                        src_str,
                        role_str,
                        outcome_str,
                        offers_cnt,
                        fs_val,
                        qw_val,
                    ),
                )
        except Exception:
            log.exception("ошибка записи метрики attempt")

    def site_recent_outcomes(self, site: str, since_ts: float) -> list[tuple[str | None, bool]]:
        """Возвращает исходы запусков сайта (device, ok) за период >= since_ts из site_runs."""
        try:
            with self._connect() as db:
                cur = db.execute(
                    """
                    SELECT device, ok, captcha, ts
                    FROM site_runs
                    WHERE site = ? AND ts >= ?
                    ORDER BY ts ASC
                    """,
                    (site, float(since_ts)),
                )
                return [
                    SiteOutcome(
                        row["device"],
                        bool(row["ok"]),
                        bool(row["captcha"]),
                        float(row["ts"]),
                    )
                    for row in cur.fetchall()
                ]
        except Exception:
            log.exception("ошибка получения недавних исходов сайта %s", site)
            return []

    def record_results(
        self,
        source: str,
        results: Sequence[SiteResult],
        ts: float | None = None,
        priority: int | None = None,
    ) -> None:
        """Записывает результаты группы сайтов из SiteResult."""
        try:
            batch_ts = time.time() if ts is None else float(ts)
            rows = []
            for r in results:
                is_captcha = bool(getattr(r, "captcha", False))
                if not is_captcha and r.error:
                    if "капч" in r.error.lower():
                        is_captcha = True
                offers_count = len(r.offers) if r.offers is not None else 0
                err_str = str(r.error)[:300] if r.error else None
                dev = getattr(r, "device", None)
                dev_str = str(dev).strip() if dev and str(dev).strip() else None
                qw = getattr(r, "queue_wait", None)
                qw_val = float(qw) if qw is not None else None
                fs = getattr(r, "fetch_seconds", None)
                fs_val = float(fs) if fs is not None else None
                prio = getattr(r, "priority", None) if priority is None else priority
                if prio is None:
                    if source == "user":
                        prio = 0
                    elif source == "sub":
                        prio = 1
                    elif source == "bg":
                        prio = 2
                prio_val = int(prio) if prio is not None else None
                rows.append((
                    batch_ts,
                    source,
                    r.site,
                    1 if r.ok else 0,
                    1 if r.partial else 0,
                    1 if is_captcha else 0,
                    err_str,
                    offers_count,
                    float(r.elapsed),
                    dev_str,
                    qw_val,
                    fs_val,
                    prio_val,
                ))
            if rows:
                with self._connect() as db:
                    db.executemany(
                        """
                        INSERT INTO site_runs (
                            ts, source, site, ok, partial, captcha, error, offers, elapsed,
                            device, queue_wait, fetch_seconds, priority
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
        except Exception:
            log.exception("ошибка записи группы метрик results")

    def summary(self, since_ts: float, source: str | None = None) -> dict[str, dict[str, Any]]:
        """Сводка по каждому сайту и '_total' за период since_ts."""
        query_parts = ["WHERE ts >= ?"]
        params: list[Any] = [float(since_ts)]
        if source is not None:
            query_parts.append("AND source = ?")
            params.append(source)
        where_clause = " ".join(query_parts)

        with self._connect() as db:
            cur = db.execute(
                f"""
                SELECT
                    site,
                    COUNT(*) AS runs,
                    SUM(ok) AS ok_cnt,
                    SUM(partial) AS partial_cnt,
                    SUM(captcha) AS captcha_cnt,
                    SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_cnt,
                    AVG(elapsed) AS avg_elapsed,
                    AVG(offers) AS avg_offers
                FROM site_runs
                {where_clause}
                GROUP BY site
                """,
                params,
            )
            sites_data = cur.fetchall()

            # Последняя ошибка по каждому сайту
            last_errors_cur = db.execute(
                f"""
                SELECT site, error, ts
                FROM (
                    SELECT site, error, ts,
                           ROW_NUMBER() OVER (PARTITION BY site ORDER BY ts DESC, id DESC) AS rn
                    FROM site_runs
                    {where_clause} AND error IS NOT NULL AND error != ''
                )
                WHERE rn = 1
                """,
                params,
            )
            last_errors = {row["site"]: (row["error"], float(row["ts"])) for row in last_errors_cur.fetchall()}

            # Общие суммы и средние
            tot_cur = db.execute(
                f"""
                SELECT
                    COUNT(*) AS runs,
                    COALESCE(SUM(ok), 0) AS ok_cnt,
                    COALESCE(SUM(partial), 0) AS partial_cnt,
                    COALESCE(SUM(captcha), 0) AS captcha_cnt,
                    COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed_cnt,
                    COALESCE(AVG(elapsed), 0.0) AS avg_elapsed,
                    COALESCE(AVG(offers), 0.0) AS avg_offers
                FROM site_runs
                {where_clause}
                """,
                params,
            )
            tot_row = tot_cur.fetchone()

            tot_err_cur = db.execute(
                f"""
                SELECT error, ts
                FROM site_runs
                {where_clause} AND error IS NOT NULL AND error != ''
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
                params,
            )
            tot_err_row = tot_err_cur.fetchone()

        result: dict[str, dict[str, Any]] = {}
        for row in sites_data:
            s_name = str(row["site"])
            last_err_text, last_err_ts = last_errors.get(s_name, (None, None))
            result[s_name] = {
                "runs": int(row["runs"]),
                "ok": int(row["ok_cnt"]),
                "partial": int(row["partial_cnt"]),
                "captcha": int(row["captcha_cnt"]),
                "failed": int(row["failed_cnt"]),
                "avg_elapsed": round(float(row["avg_elapsed"]), 2) if row["avg_elapsed"] is not None else 0.0,
                "avg_offers": round(float(row["avg_offers"]), 2) if row["avg_offers"] is not None else 0.0,
                "last_error": last_err_text,
                "last_error_ts": last_err_ts,
            }

        total_runs = int(tot_row["runs"]) if tot_row else 0
        result["_total"] = {
            "runs": total_runs,
            "ok": int(tot_row["ok_cnt"]) if tot_row else 0,
            "partial": int(tot_row["partial_cnt"]) if tot_row else 0,
            "captcha": int(tot_row["captcha_cnt"]) if tot_row else 0,
            "failed": int(tot_row["failed_cnt"]) if tot_row else 0,
            "avg_elapsed": round(float(tot_row["avg_elapsed"]), 2) if tot_row and total_runs > 0 else 0.0,
            "avg_offers": round(float(tot_row["avg_offers"]), 2) if tot_row and total_runs > 0 else 0.0,
            "last_error": tot_err_row["error"] if tot_err_row else None,
            "last_error_ts": float(tot_err_row["ts"]) if tot_err_row else None,
        }
        return result

    def summary_by_device(
        self,
        since_ts: float,
        source: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Сводка по каждому телефону за период since_ts."""
        query_parts = ["WHERE ts >= ? AND device IS NOT NULL AND device != ''"]
        params: list[Any] = [float(since_ts)]
        if source is not None:
            query_parts.append("AND source = ?")
            params.append(source)
        where_clause = " ".join(query_parts)

        with self._connect() as db:
            cur = db.execute(
                f"""
                SELECT
                    device,
                    COUNT(*) AS runs,
                    SUM(ok) AS ok_cnt,
                    SUM(partial) AS partial_cnt,
                    SUM(captcha) AS captcha_cnt,
                    SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_cnt,
                    AVG(fetch_seconds) AS avg_fetch,
                    AVG(queue_wait) AS avg_queue_wait
                FROM site_runs
                {where_clause}
                GROUP BY device
                ORDER BY device
                """,
                params,
            )
            rows = cur.fetchall()

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            dev = str(row["device"])
            result[dev] = {
                "runs": int(row["runs"]),
                "ok": int(row["ok_cnt"] or 0),
                "partial": int(row["partial_cnt"] or 0),
                "captcha": int(row["captcha_cnt"] or 0),
                "failed": int(row["failed_cnt"] or 0),
                "avg_fetch": round(float(row["avg_fetch"]), 2) if row["avg_fetch"] is not None else 0.0,
                "avg_queue_wait": round(float(row["avg_queue_wait"]), 2) if row["avg_queue_wait"] is not None else 0.0,
            }
        return result

    def summary_by_site_device(
        self,
        since_ts: float,
        source: str | None = None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Сводка по парам (сайт, телефон) за период since_ts."""
        query_parts = ["WHERE ts >= ? AND device IS NOT NULL AND device != ''"]
        params: list[Any] = [float(since_ts)]
        if source is not None:
            query_parts.append("AND source = ?")
            params.append(source)
        where_clause = " ".join(query_parts)

        with self._connect() as db:
            cur = db.execute(
                f"""
                SELECT
                    site,
                    device,
                    COUNT(*) AS runs,
                    SUM(ok) AS ok_cnt,
                    SUM(partial) AS partial_cnt,
                    SUM(captcha) AS captcha_cnt,
                    SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_cnt,
                    AVG(fetch_seconds) AS avg_fetch,
                    AVG(queue_wait) AS avg_queue_wait
                FROM site_runs
                {where_clause}
                GROUP BY site, device
                ORDER BY runs DESC, site, device
                """,
                params,
            )
            rows = cur.fetchall()

        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["site"]), str(row["device"]))
            result[key] = {
                "runs": int(row["runs"]),
                "ok": int(row["ok_cnt"] or 0),
                "partial": int(row["partial_cnt"] or 0),
                "captcha": int(row["captcha_cnt"] or 0),
                "failed": int(row["failed_cnt"] or 0),
                "avg_fetch": round(float(row["avg_fetch"]), 2) if row["avg_fetch"] is not None else 0.0,
                "avg_queue_wait": round(float(row["avg_queue_wait"]), 2) if row["avg_queue_wait"] is not None else 0.0,
            }
        return result

    def recent_errors(self, limit: int = 10, since_ts: float | None = None) -> list[dict[str, Any]]:
        """Последние ошибки поиска по всем сайтам."""
        query_parts = ["WHERE error IS NOT NULL AND error != ''"]
        params: list[Any] = []
        if since_ts is not None:
            query_parts.append("AND ts >= ?")
            params.append(float(since_ts))
        where_clause = " ".join(query_parts)
        params.append(int(limit))

        with self._connect() as db:
            cur = db.execute(
                f"""
                SELECT ts, site, source, error
                FROM site_runs
                {where_clause}
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            return [
                {
                    "ts": float(row["ts"]),
                    "site": str(row["site"]),
                    "source": str(row["source"]),
                    "error": str(row["error"]),
                }
                for row in cur.fetchall()
            ]

    def counts_by_source(self, since_ts: float) -> dict[str, int]:
        """Количество записей с разбивкой по source за период."""
        with self._connect() as db:
            cur = db.execute(
                """
                SELECT source, COUNT(*) AS cnt
                FROM site_runs
                WHERE ts >= ?
                GROUP BY source
                """,
                (float(since_ts),),
            )
            return {str(row["source"]): int(row["cnt"]) for row in cur.fetchall()}

    def prune(self, keep_days: int = 90, now: float | None = None) -> int:
        """Удаляет устаревшие метрики старше keep_days дней."""
        now_ts = time.time() if now is None else float(now)
        cutoff = now_ts - keep_days * 86400.0
        with self._connect() as db:
            cur = db.execute("DELETE FROM site_runs WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def watch_stats(self, since_ts: float) -> dict[str, dict[str, Any]]:
        """Статистика запусков по сайтам для parser_watch за период since_ts."""
        with self._connect() as db:
            cur = db.execute(
                """
                SELECT
                    site,
                    COUNT(*) AS runs,
                    SUM(ok) AS ok_cnt,
                    SUM(partial) AS partial_cnt
                FROM site_runs
                WHERE ts >= ?
                GROUP BY site
                """,
                (float(since_ts),),
            )
            res = {}
            for row in cur.fetchall():
                res[str(row["site"])] = {
                    "runs": int(row["runs"]),
                    "ok": int(row["ok_cnt"] or 0),
                    "partial": int(row["partial_cnt"] or 0),
                }
            return res

    def get_watch_state(self, site: str) -> dict[str, Any] | None:
        """Возвращает сохранённое состояние алерта parser_watch для сайта."""
        with self._connect() as db:
            cur = db.execute(
                "SELECT site, last_status, last_alert_ts, last_alert_status FROM parser_watch_state WHERE site = ?",
                (site,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "site": str(row["site"]),
                "last_status": str(row["last_status"]),
                "last_alert_ts": float(row["last_alert_ts"]),
                "last_alert_status": str(row["last_alert_status"]),
            }

    def set_watch_state(
        self,
        site: str,
        last_status: str,
        last_alert_ts: float,
        last_alert_status: str,
    ) -> None:
        """Сохраняет состояние алерта parser_watch для сайта."""
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO parser_watch_state (site, last_status, last_alert_ts, last_alert_status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(site) DO UPDATE SET
                    last_status = excluded.last_status,
                    last_alert_ts = excluded.last_alert_ts,
                    last_alert_status = excluded.last_alert_status
                """,
                (site, last_status, float(last_alert_ts), last_alert_status),
            )


def _recent_by_pair(self, since_ts: float, per_pair: int = 5) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Последние запуски по каждой паре (сайт, телефон): новые первыми, не больше per_pair."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    with self._connect() as db:
        cur = db.execute(
            """
            SELECT ts, site, device, ok, partial, captcha, error, offers
            FROM site_runs
            WHERE ts >= ? AND device IS NOT NULL AND device != '' AND device != 'unknown'
            ORDER BY ts DESC
            """,
            (float(since_ts),),
        )
        for row in cur.fetchall():
            key = (str(row["site"]), str(row["device"]))
            lst = out.setdefault(key, [])
            if len(lst) < per_pair:
                lst.append(
                    {
                        "ts": float(row["ts"]),
                        "ok": int(row["ok"]),
                        "partial": int(row["partial"]),
                        "captcha": int(row["captcha"]),
                        "error": str(row["error"] or ""),
                        "offers": int(row["offers"] or 0),
                    }
                )
    return out


MetricsStore.recent_by_pair = _recent_by_pair  # type: ignore[attr-defined]


def _record_search(
    self,
    source: str,
    total_seconds: float,
    results: Sequence[SiteResult],
    queue_seconds: float | None = None,
    ts: float | None = None,
) -> None:
    """Итог всего поиска: сколько ждал человек, когда пришла первая цена, сколько сайтов ответило."""
    try:
        oks = [float(r.elapsed) for r in results if r.ok and r.offers]
        row = (
            time.time() if ts is None else float(ts),
            source,
            float(total_seconds),
            min(oks) if oks else None,
            len(results),
            len(oks),
            float(queue_seconds) if queue_seconds is not None else None,
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO searches (ts, source, total_seconds, first_seconds, sites_total, sites_ok, queue_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
    except Exception:
        log.exception("ошибка записи итога поиска")


MetricsStore.record_search = _record_search  # type: ignore[attr-defined]
