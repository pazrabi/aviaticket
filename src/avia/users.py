"""Пользователи бота: регистрация по Telegram id и дневной лимит поисков (SQLite)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    created_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    blocked       INTEGER NOT NULL DEFAULT 0,
    search_day    TEXT,
    search_count  INTEGER NOT NULL DEFAULT 0,
    total_searches INTEGER NOT NULL DEFAULT 0
);
"""


def _search_day(ts: float) -> str:
    """Местные сутки, за которые засчитан поиск (users.search_day)."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@dataclass
class User:
    tg_id: int
    username: str | None
    first_name: str | None
    created_at: float
    last_seen_at: float
    blocked: bool
    total_searches: int


class UserStore:
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
    def _row(row: sqlite3.Row) -> User:
        return User(
            tg_id=int(row["tg_id"]),
            username=row["username"],
            first_name=row["first_name"],
            created_at=float(row["created_at"]),
            last_seen_at=float(row["last_seen_at"]),
            blocked=bool(row["blocked"]),
            total_searches=int(row["total_searches"]),
        )

    def register(self, tg_id: int, username: str | None = None, first_name: str | None = None,
                 now: float | None = None) -> User:
        """Создаёт пользователя при первом обращении, иначе обновляет имя и время визита."""
        ts = time.time() if now is None else now
        with self._connect() as db:
            db.execute(
                "INSERT INTO users (tg_id, username, first_name, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, "
                "first_name = excluded.first_name, last_seen_at = excluded.last_seen_at",
                (int(tg_id), username, first_name, ts, ts),
            )
            row = db.execute("SELECT * FROM users WHERE tg_id = ?", (int(tg_id),)).fetchone()
        return self._row(row)

    def get(self, tg_id: int) -> User | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE tg_id = ?", (int(tg_id),)).fetchone()
        return self._row(row) if row else None

    def set_blocked(self, tg_id: int, blocked: bool) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE users SET blocked = ? WHERE tg_id = ?", (1 if blocked else 0, int(tg_id)))
        return cur.rowcount > 0

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def try_use_search(self, tg_id: int, daily_limit: int, now: float | None = None) -> bool:
        """Засчитывает поиск за сегодня; False, если дневной лимит исчерпан (0 = без лимита)."""
        ts = time.time() if now is None else now
        day = _search_day(ts)
        with self._connect() as db:
            row = db.execute("SELECT search_day, search_count FROM users WHERE tg_id = ?", (int(tg_id),)).fetchone()
            if row is None:
                return True
            count = int(row["search_count"]) if row["search_day"] == day else 0
            if daily_limit > 0 and count >= daily_limit:
                return False
            db.execute(
                "UPDATE users SET search_day = ?, search_count = ?, total_searches = total_searches + 1 "
                "WHERE tg_id = ?",
                (day, count + 1, int(tg_id)),
            )
        return True

    def refund_search(self, tg_id: int, charged_at: float) -> None:
        """Возвращает попытку, которую try_use_search списал в момент charged_at.

        Счёт дня уменьшается, только если в строке ещё день списания: поиск на стыке суток
        не трогает счёт нового дня. Всего поисков уменьшается в любом случае. Оба не ниже 0;
        нет строки — ничего не делает.
        """
        with self._connect() as db:
            db.execute(
                "UPDATE users SET "
                "search_count = CASE WHEN search_day = ? AND search_count > 0 "
                "THEN search_count - 1 ELSE search_count END, "
                "total_searches = MAX(total_searches - 1, 0) "
                "WHERE tg_id = ?",
                (_search_day(charged_at), int(tg_id)),
            )

    def stats(self, today: str | None = None) -> dict[str, int]:
        """Сводка для администратора: людей всего, активных сегодня, заблокированных, поисков всего."""
        day = today or datetime.now().strftime("%Y-%m-%d")
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(blocked), 0) AS b, COALESCE(SUM(total_searches), 0) AS s, "
                "COALESCE(SUM(CASE WHEN search_day = ? THEN search_count ELSE 0 END), 0) AS today "
                "FROM users",
                (day,),
            ).fetchone()
        return {"users": int(row["n"]), "blocked": int(row["b"]), "searches": int(row["s"]), "today": int(row["today"])}

    def recent(self, limit: int = 10) -> list[User]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def count_since_created(self, ts: float) -> int:
        """Число пользователей, созданных начиная с ts."""
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (float(ts),)).fetchone()
            return int(row[0]) if row else 0

    def count_active_since(self, ts: float) -> int:
        """Число пользователей, заходивших начиная с ts."""
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) FROM users WHERE last_seen_at >= ?", (float(ts),)).fetchone()
            return int(row[0]) if row else 0

    def top_searchers(self, limit: int = 5) -> list[User]:
        """Топ пользователей по суммарному числу поисков."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM users ORDER BY total_searches DESC, last_seen_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [self._row(r) for r in rows]

