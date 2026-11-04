from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Sequence

from .criteria import SearchCriteria
from .models import Query

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id                INTEGER NOT NULL,
    origin                 TEXT NOT NULL,
    destination            TEXT NOT NULL,
    depart                 TEXT NOT NULL,
    return_date            TEXT,
    adults                 INTEGER NOT NULL,
    baggage_mode           TEXT NOT NULL,
    criteria               TEXT NOT NULL,
    threshold              INTEGER,
    active                 INTEGER NOT NULL DEFAULT 1,
    created_at             REAL NOT NULL,
    last_checked_at        REAL,
    last_notified_at       REAL,
    last_notified_price    INTEGER,
    last_notified_offer_id TEXT,
    min_price              INTEGER
);
CREATE INDEX IF NOT EXISTS ix_subscriptions_chat_active ON subscriptions (chat_id, active);

CREATE TABLE IF NOT EXISTS sub_prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id      INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    offer_id    TEXT,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sub_prices_sub_obs ON sub_prices (sub_id, observed_at);

CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id   INTEGER NOT NULL,
    offer_id TEXT,
    price    INTEGER NOT NULL,
    reasons  TEXT NOT NULL,
    sent_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notifications_sub ON notifications (sub_id, sent_at);
"""


def criteria_to_json(c: SearchCriteria) -> str:
    """Сериализует SearchCriteria в JSON строку (части суток в отсортированный список)."""
    return json.dumps(
        {
            "arrive_parts": sorted(c.arrive_parts),
            "depart_parts": sorted(c.depart_parts),
            "hour_cost": c.hour_cost,
            "max_duration_hours": c.max_duration_hours,
            "max_transfers": c.max_transfers,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def criteria_from_json(s: str) -> SearchCriteria:
    """Десериализует SearchCriteria из JSON строки; незнакомые ключи и мусор игнорирует."""
    if not isinstance(s, str):
        return SearchCriteria()
    try:
        data = json.loads(s)
        if not isinstance(data, dict):
            return SearchCriteria()

        max_transfers = data.get("max_transfers")
        if not isinstance(max_transfers, int) or isinstance(max_transfers, bool):
            max_transfers = None

        max_duration_hours = data.get("max_duration_hours")
        if not isinstance(max_duration_hours, int) or isinstance(max_duration_hours, bool):
            max_duration_hours = None

        hour_cost = data.get("hour_cost")
        if not isinstance(hour_cost, int) or isinstance(hour_cost, bool):
            hour_cost = None

        raw_depart = data.get("depart_parts")
        if isinstance(raw_depart, (list, tuple, set, frozenset)):
            depart_parts = frozenset(x for x in raw_depart if isinstance(x, str))
        else:
            depart_parts = frozenset()

        raw_arrive = data.get("arrive_parts")
        if isinstance(raw_arrive, (list, tuple, set, frozenset)):
            arrive_parts = frozenset(x for x in raw_arrive if isinstance(x, str))
        else:
            arrive_parts = frozenset()

        return SearchCriteria(
            max_transfers=max_transfers,
            max_duration_hours=max_duration_hours,
            depart_parts=depart_parts,
            arrive_parts=arrive_parts,
            hour_cost=hour_cost,
        )
    except Exception:
        return SearchCriteria()


@dataclass
class Subscription:
    id: int
    chat_id: int
    origin: str
    destination: str
    depart: date
    return_date: date | None = None
    adults: int = 1
    baggage_mode: str = "any"
    criteria: SearchCriteria = field(default_factory=SearchCriteria)
    threshold: int | None = None
    active: bool = True
    created_at: float = 0.0
    last_checked_at: float | None = None
    last_notified_at: float | None = None
    last_notified_price: int | None = None
    last_notified_offer_id: str | None = None
    min_price: int | None = None

    def query(self) -> Query:
        return Query(
            origin=self.origin,
            destination=self.destination,
            depart=self.depart,
            adults=self.adults,
            return_date=self.return_date,
        )

    def group_key(self) -> tuple[str, str, date, date | None, int]:
        return (self.origin, self.destination, self.depart, self.return_date, self.adults)


def poll_interval_seconds(depart: date, today: date) -> int:
    """Интервал опроса подписки: >30 дней: 3600; 8-30 дней: 2700; <=7 дней: 1800."""
    days = (depart - today).days
    if days > 30:
        return 3600
    if days >= 8:
        return 2700
    return 1800


class SubscriptionStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_sub(row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            origin=str(row["origin"]),
            destination=str(row["destination"]),
            depart=date.fromisoformat(row["depart"]),
            return_date=date.fromisoformat(row["return_date"]) if row["return_date"] else None,
            adults=int(row["adults"]),
            baggage_mode=str(row["baggage_mode"]),
            criteria=criteria_from_json(row["criteria"]),
            threshold=int(row["threshold"]) if row["threshold"] is not None else None,
            active=bool(row["active"]),
            created_at=float(row["created_at"]),
            last_checked_at=float(row["last_checked_at"]) if row["last_checked_at"] is not None else None,
            last_notified_at=float(row["last_notified_at"]) if row["last_notified_at"] is not None else None,
            last_notified_price=int(row["last_notified_price"]) if row["last_notified_price"] is not None else None,
            last_notified_offer_id=(
                str(row["last_notified_offer_id"])
                if row["last_notified_offer_id"] is not None
                else None
            ),
            min_price=int(row["min_price"]) if row["min_price"] is not None else None,
        )

    def create(
        self,
        chat_id: int,
        query: Query,
        baggage_mode: str,
        criteria: SearchCriteria,
        now: float | None = None,
    ) -> Subscription:
        now = time.time() if now is None else float(now)
        chat_id = int(chat_id)
        depart_str = query.depart.isoformat()
        return_str = query.return_date.isoformat() if query.return_date else None

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM subscriptions
                WHERE chat_id = ?
                  AND active = 1
                  AND origin = ?
                  AND destination = ?
                  AND depart = ?
                  AND ((return_date IS NULL AND ? IS NULL) OR return_date = ?)
                  AND adults = ?
                  AND baggage_mode = ?
                """,
                (
                    chat_id,
                    query.origin,
                    query.destination,
                    depart_str,
                    return_str,
                    return_str,
                    query.adults,
                    baggage_mode,
                ),
            ).fetchall()
            for row in rows:
                existing_sub = self._row_to_sub(row)
                if existing_sub.criteria == criteria:
                    return existing_sub

            cur = db.execute(
                """
                INSERT INTO subscriptions (
                    chat_id, origin, destination, depart, return_date,
                    adults, baggage_mode, criteria, threshold, active,
                    created_at, last_checked_at, last_notified_at,
                    last_notified_price, last_notified_offer_id, min_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    chat_id,
                    query.origin,
                    query.destination,
                    depart_str,
                    return_str,
                    query.adults,
                    baggage_mode,
                    criteria_to_json(criteria),
                    now,
                ),
            )
            sub_id = int(cur.lastrowid)

        return Subscription(
            id=sub_id,
            chat_id=chat_id,
            origin=query.origin,
            destination=query.destination,
            depart=query.depart,
            return_date=query.return_date,
            adults=query.adults,
            baggage_mode=baggage_mode,
            criteria=criteria,
            threshold=None,
            active=True,
            created_at=now,
            last_checked_at=None,
            last_notified_at=None,
            last_notified_price=None,
            last_notified_offer_id=None,
            min_price=None,
        )

    def get(self, sub_id: int) -> Subscription | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (sub_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_sub(row)

    def list_active(self, chat_id: int) -> list[Subscription]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM subscriptions WHERE chat_id = ? AND active = 1 ORDER BY id ASC",
                (int(chat_id),),
            ).fetchall()
            return [self._row_to_sub(row) for row in rows]

    def count_active(self, chat_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ? AND active = 1",
                (int(chat_id),),
            ).fetchone()
            return int(row[0]) if row else 0

    def deactivate(self, sub_id: int, chat_id: int) -> bool:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE subscriptions SET active = 0 WHERE id = ? AND chat_id = ? AND active = 1",
                (sub_id, int(chat_id)),
            )
            return cur.rowcount > 0

    def set_threshold(self, sub_id: int, chat_id: int, price: int | None) -> bool:
        thresh = int(price) if price is not None else None
        with self._connect() as db:
            cur = db.execute(
                "UPDATE subscriptions SET threshold = ? WHERE id = ? AND chat_id = ? AND active = 1",
                (thresh, sub_id, int(chat_id)),
            )
            return cur.rowcount > 0

    def expire_past(self, today: date) -> int:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE subscriptions SET active = 0 WHERE active = 1 AND depart < ?",
                (today.isoformat(),),
            )
            return int(cur.rowcount)

    def due(self, now: float, today: date) -> list[Subscription]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM subscriptions WHERE active = 1 ORDER BY id ASC"
            ).fetchall()
        result: list[Subscription] = []
        for row in rows:
            sub = self._row_to_sub(row)
            interval = poll_interval_seconds(sub.depart, today)
            if sub.last_checked_at is None or (now - sub.last_checked_at) >= interval:
                result.append(sub)
        return result

    def mark_checked(self, sub_id: int, now: float) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE subscriptions SET last_checked_at = ? WHERE id = ?",
                (float(now), sub_id),
            )

    def record_price(
        self,
        sub_id: int,
        price: int,
        currency: str,
        offer_id: str | None,
        now: float,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO sub_prices (sub_id, price, currency, offer_id, observed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sub_id, int(price), currency, offer_id, float(now)),
            )
            db.execute(
                """
                UPDATE subscriptions
                SET min_price = (SELECT MIN(price) FROM sub_prices WHERE sub_id = ?)
                WHERE id = ?
                """,
                (sub_id, sub_id),
            )

    def recent_prices(self, sub_id: int, since: float) -> list[int]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT price FROM sub_prices
                WHERE sub_id = ? AND observed_at >= ?
                ORDER BY observed_at ASC, id ASC
                """,
                (sub_id, float(since)),
            ).fetchall()
            return [int(row["price"]) for row in rows]

    def first_price(self, sub_id: int) -> int | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT price FROM sub_prices
                WHERE sub_id = ?
                ORDER BY observed_at ASC, id ASC
                LIMIT 1
                """,
                (sub_id,),
            ).fetchone()
            if row is None:
                return None
            return int(row["price"])

    def record_notification(
        self,
        sub_id: int,
        offer_id: str | None,
        price: int,
        reasons: list[str],
        now: float,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO notifications (sub_id, offer_id, price, reasons, sent_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sub_id, offer_id, int(price), json.dumps(reasons, ensure_ascii=False), float(now)),
            )
            db.execute(
                """
                UPDATE subscriptions
                SET last_notified_at = ?,
                    last_notified_price = ?,
                    last_notified_offer_id = ?
                WHERE id = ?
                """,
                (float(now), int(price), offer_id, sub_id),
            )

    def notification_reason_counts(self, since_ts: float) -> dict[str, int]:
        """Группирует причины отправленных уведомлений с since_ts по типу."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT reasons FROM notifications WHERE sent_at >= ? ORDER BY id ASC",
                (float(since_ts),),
            ).fetchall()

        counts: dict[str, int] = {}
        for (raw,) in rows:
            if not raw:
                continue
            reasons_list: list[str] = []
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    reasons_list = [str(x) for x in parsed]
                elif isinstance(parsed, str):
                    reasons_list = [parsed]
            except Exception:
                reasons_list = [part.strip() for part in raw.replace(";", "\n").splitlines() if part.strip()]

            for r in reasons_list:
                key = _classify_reason(r)
                counts[key] = counts.get(key, 0) + 1
        return counts

    def overdue_stats(self, now: float, today: date) -> dict[str, int]:
        """Статистика очереди проверок: число активных, просроченных и макс. отставание в мин."""
        today_date = today() if callable(today) else today
        now_ts = now() if callable(now) else float(now)

        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM subscriptions WHERE active = 1 ORDER BY id ASC"
            ).fetchall()

        total = len(rows)
        overdue_count = 0
        max_lag_seconds = 0.0

        for row in rows:
            sub = self._row_to_sub(row)
            interval = poll_interval_seconds(sub.depart, today_date)
            ref_time = sub.last_checked_at if sub.last_checked_at is not None else sub.created_at
            elapsed = now_ts - ref_time
            if elapsed > interval:
                overdue_count += 1
                lag = elapsed - interval
                if lag > max_lag_seconds:
                    max_lag_seconds = lag

        max_lag_minutes = int(round(max_lag_seconds / 60.0))
        return {
            "total": total,
            "active": total,
            "overdue": overdue_count,
            "max_lag_minutes": max_lag_minutes,
            "max_lag": max_lag_minutes,
        }


def _classify_reason(s: str) -> str:
    """Определяет тип причины уведомления без чисел."""
    s_clean = s.strip().lower()
    if s_clean.startswith("новый минимум"):
        return "новый минимум"
    if s_clean.startswith("упала"):
        return "упала"
    if s_clean.startswith("подорожала"):
        return "подорожала"
    if "порог" in s_clean:
        return "ниже порога"
    if "ниже обычного" in s_clean:
        return "ниже обычного"
    return s.strip()


@dataclass
class NotifyRules:
    drop_pct: float = 5.0
    drop_rub: int = 500
    median_pct: float = 15.0
    median_min_points: int = 5
    pause_seconds: float = 3 * 3600.0
    median_window_seconds: float = 14 * 86400.0
    rise_pct: float = 10.0


_DEFAULT_RULES = NotifyRules()


def evaluate(
    sub: Subscription,
    price: int,
    offer_id: str | None,
    now: float,
    recent_prices: list[int],
    rules: NotifyRules = _DEFAULT_RULES,
    reference_price: int | None = None,
) -> list[str]:
    """Вычисляет причины для уведомления пользователя по подписке."""
    if isinstance(rules, (int, float)) and reference_price is None:
        reference_price = int(rules)
        active_rules = _DEFAULT_RULES
    elif rules is None:
        active_rules = _DEFAULT_RULES
    else:
        active_rules = rules

    # Пауза: если с последнего уведомления прошло меньше pause_seconds — вернуть []
    if sub.last_notified_at is not None and (now - sub.last_notified_at) < active_rules.pause_seconds:
        return []

    # Дедупликация: если offer_id и price совпадают с last_notified_offer_id и last_notified_price — вернуть []
    if (
        sub.last_notified_price is not None
        and price == sub.last_notified_price
        and offer_id == sub.last_notified_offer_id
    ):
        return []

    reasons: list[str] = []

    # 1. «новый минимум»: sub.min_price известен и price < sub.min_price
    if sub.min_price is not None and price < sub.min_price:
        reasons.append("новый минимум")

    # 2. «упала на N ₽ (P%)»: относительно эталона = sub.last_notified_price, а если уведомлений
    # ещё не было, то первой записанной цены подписки (reference_price).
    ref = sub.last_notified_price if sub.last_notified_price is not None else reference_price
    if ref is not None and ref > 0 and price < ref:
        diff = ref - price
        pct = (diff / ref) * 100.0
        if diff >= active_rules.drop_rub or pct >= active_rules.drop_pct:
            reasons.append(f"упала на {diff} ₽ ({round(pct)}%)")

    # 3. «ниже вашего порога N ₽»: sub.threshold задан и price <= threshold
    if sub.threshold is not None and price <= sub.threshold:
        reasons.append(f"ниже вашего порога {sub.threshold} ₽")

    # 4. «на N% ниже обычного»: в recent_prices не меньше median_min_points значений и price ниже медианы на >= median_pct %
    if len(recent_prices) >= active_rules.median_min_points:
        med = statistics.median(recent_prices)
        if med > 0 and price < med:
            med_pct = (med - price) / med * 100.0
            if med_pct >= active_rules.median_pct:
                reasons.append(f"на {round(med_pct)}% ниже обычного")

    # 5. «подорожала на N ₽ (P%)»: относительно эталона ref (sub.last_notified_price или reference_price)
    if (
        ref is not None
        and ref > 0
        and price > ref
        and "новый минимум" not in reasons
    ):
        diff = price - ref
        pct = (diff / ref) * 100.0
        if (price * 100) >= round(ref * (100.0 + active_rules.rise_pct), 6):
            reasons.append(f"подорожала на {diff} ₽ ({round(pct)}%)")

    return reasons
