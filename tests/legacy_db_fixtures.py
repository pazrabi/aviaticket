"""Четыре старые базы state/*.sqlite для тестов КТ4, работа 1 (одна база, DB_SINGLE).

Схемы — те, что создают сторы владельца (UserStore, SubscriptionStore, PriceHistory, MetricsStore).
Люди и подписки — через API сторов: их потом читает код бота. Остальные таблицы — общие строки
по PRAGMA table_info: в каждой колонке своё значение на строку, так что сравнение строк ловит
перепутанные колонки и потерянные записи.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sqlite3

from avia.criteria import SearchCriteria
from avia.history import PriceHistory
from avia.metrics import SEARCHES_CARD_COLUMNS, SEARCHES_PREVIEW_COLUMNS, MetricsStore
from avia.models import Query
from avia.subscriptions import SubscriptionStore
from avia.users import UserStore

LEGACY = ("users", "subscriptions", "history", "metrics")
GENERIC_ROWS = 7
KT2_COLUMNS = tuple(col for col, _ in SEARCHES_CARD_COLUMNS + SEARCHES_PREVIEW_COLUMNS)
PEOPLE = (101, 102, 103)
BASE_TS = 1_790_000_000.0


def _value(table: str, col: str, decl: str, i: int) -> object:
    t = (decl or "").upper()
    if "INT" in t:
        return 1000 * i + len(col)
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return BASE_TS + i + len(col) / 100
    if "BLOB" in t:
        return f"{table}:{col}:{i}".encode()
    return f"{table}:{col}:{i}"


def fill_generic(db_path: Path, table: str, n: int = GENERIC_ROWS) -> None:
    """n строк в table: INTEGER PRIMARY KEY — авто, остальное — своё значение на колонку и строку."""
    with sqlite3.connect(db_path) as db:
        cols = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        names = [c[1] for c in cols if not (c[5] == 1 and "INT" in (c[2] or "").upper())]
        decl = {c[1]: c[2] for c in cols}
        marks = ", ".join("?" for _ in names)
        sql = f'INSERT INTO "{table}" ({", ".join(names)}) VALUES ({marks})'
        db.executemany(sql, [[_value(table, c, decl[c], i) for c in names] for i in range(1, n + 1)])


def _people(path: Path) -> None:
    users = UserStore(path)
    for i, tg_id in enumerate(PEOPLE):
        users.register(tg_id, username=f"user{tg_id}", first_name=f"Имя{tg_id}", now=BASE_TS + i)
    users.charge_search(PEOPLE[0], daily_limit=5, now=BASE_TS + 10)
    users.set_blocked(PEOPLE[2], True)


def _subscriptions(path: Path) -> None:
    subs = SubscriptionStore(path)
    depart = date.today() + timedelta(days=40)
    for i, chat in enumerate(PEOPLE):
        q = Query(origin="MOW", destination="AER", depart=depart + timedelta(days=i))
        sub = subs.create(chat, q, "any", SearchCriteria(max_transfers=i), now=BASE_TS + i)
        subs.record_price(sub.id, 5000 + i, "RUB", f"offer-{i}", BASE_TS + 100 + i)
    # верхний id удалён: sqlite_sequence помнит 3, следующая подписка — 4, а не 3
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM subscriptions WHERE id = (SELECT MAX(id) FROM subscriptions)")
    fill_generic(path, "notifications")


def build_legacy_state(state_dir: Path, *, kt2_columns: bool = True) -> None:
    """Четыре файла в state_dir: схемы сторов владельца и строки в каждой таблице.

    kt2_columns=False — metrics.sqlite до П31/П32: в searches нет card_* и preview_*.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    _people(state_dir / "users.sqlite")
    _subscriptions(state_dir / "subscriptions.sqlite")
    PriceHistory(state_dir / "history.sqlite")
    for table in ("price_history", "route_daily", "scheduler_heartbeat", "bg_collect_slots"):
        fill_generic(state_dir / "history.sqlite", table)
    metrics = MetricsStore(state_dir / "metrics.sqlite")
    metrics._connect().close()
    for table in ("site_runs", "attempts", "searches", "parser_watch_state", "price_timeline"):
        fill_generic(state_dir / "metrics.sqlite", table)
    if not kt2_columns:
        with sqlite3.connect(state_dir / "metrics.sqlite") as db:
            for col in KT2_COLUMNS:
                db.execute(f"ALTER TABLE searches DROP COLUMN {col}")


def user_tables(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as db:
        return [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def table_rows(db_path: Path, table: str) -> list[tuple]:
    """Все строки таблицы по именам колонок (порядок колонок не важен), отсортированные."""
    with sqlite3.connect(db_path) as db:
        cols = sorted(r[1] for r in db.execute(f'PRAGMA table_info("{table}")'))
        rows = db.execute(f'SELECT {", ".join(cols)} FROM "{table}"').fetchall()
    return sorted(rows, key=repr)


def legacy_contents(state_dir: Path) -> dict[str, list[tuple]]:
    """{таблица: строки} по всем четырём файлам."""
    out: dict[str, list[tuple]] = {}
    for name in LEGACY:
        path = state_dir / f"{name}.sqlite"
        if path.exists():
            for table in user_tables(path):
                out[table] = table_rows(path, table)
    return out


def sequences(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as db:
        try:
            return dict(db.execute("SELECT name, seq FROM sqlite_sequence"))
        except sqlite3.OperationalError:
            return {}
