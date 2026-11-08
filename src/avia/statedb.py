"""Одна база state/avia.sqlite вместо четырёх (КТ4, работа 1, ADR-002). Флаг DB_SINGLE, по умолчанию 0.

DB_SINGLE=0 — как у владельца: users, subscriptions, history, metrics — четыре файла, db_path отдаёт их же.
DB_SINGLE=1 — все сторы открывают avia.sqlite (WAL, busy_timeout 10 с — тот же timeout=10, что у сторов).
При первом обращении база создаётся и в неё переносятся четыре старых файла — в одной транзакции.
Старые файлы открываются только на чтение и остаются как были; повторный старт ничего не переносит.
Номер версии схемы — PRAGMA user_version, миграции применяются по номеру (MIGRATIONS).
Откат — export_legacy (python -m avia.cli db-rollback): всё из avia.sqlite обратно в четыре файла.
Если старый файл изменился после переноса (DB_SINGLE переключили 1 → 0 → 1 без отката), avia.sqlite
не открывается: MigrationError с тем, что делать, а не тихий старт на устаревших данных.
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable

log = logging.getLogger("avia.statedb")

SINGLE_DB_NAME = "avia.sqlite"
LEGACY_DBS = ("users", "subscriptions", "history", "metrics")
BUSY_TIMEOUT_MS = 10_000
# таблицы КТ4: в старые файлы при откате не выгружаются (при DB_SINGLE=0 сессии и выдачи — в памяти)
KT4_TABLES = frozenset({"db_meta", "db_legacy_tables", "chat_sessions", "chat_snapshots"})

_READY: set[str] = set()  # абсолютные пути avia.sqlite, уже приведённые к SCHEMA_VERSION в этом процессе
_LOCK = threading.Lock()


class MigrationError(RuntimeError):
    """Перенос или выгрузка остановлены: база в состоянии, которое код не берётся угадывать."""


def db_single_enabled() -> bool:
    return os.getenv("DB_SINGLE", "0").strip() == "1"


def single_db_path(storage_dir: Path | str) -> Path:
    return Path(storage_dir) / SINGLE_DB_NAME


def db_path(storage_dir: Path | str, name: str) -> Path:
    """Путь базы name (users, subscriptions, history, metrics): при DB_SINGLE=1 — avia.sqlite, перенос уже сделан."""
    if name not in LEGACY_DBS:
        raise ValueError(f"неизвестная база {name!r}; есть: {', '.join(LEGACY_DBS)}")
    if not db_single_enabled():
        return Path(storage_dir) / f"{name}.sqlite"
    return ensure_single_db(storage_dir)


def connect(path: Path | str, **kwargs) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, **kwargs)


def forget_ready() -> None:
    """Забыть, какие базы уже проверены (тесты: тот же путь, новый каталог)."""
    with _LOCK:
        _READY.clear()


def ensure_single_db(storage_dir: Path | str) -> Path:
    """avia.sqlite с последней версией схемы; при первом вызове — перенос четырёх старых файлов."""
    target = single_db_path(storage_dir)
    key = os.path.abspath(target)
    if key in _READY and target.exists():
        return target
    with _LOCK:
        if key not in _READY or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            _migrate(target)
            _READY.add(key)
    return target


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _main_tables(conn: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def _migrate(target: Path) -> None:
    conn = connect(target.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        version = _user_version(conn)
        if version >= 1:
            _check_legacy_unchanged(conn, target)
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise MigrationError(f"{target.name}: версия схемы {version} новее кода ({SCHEMA_VERSION}); "
                                 "откат кода без отката базы не поддерживается")
        if version == 0 and _main_tables(conn):
            raise MigrationError(f"{target.name} без номера версии, но с таблицами: перенос остановлен, "
                                 "файл не тронут")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise MigrationError(f"{target.name}: не включился WAL (journal_mode={mode})")
        attached = _attach_legacy(conn, target.parent) if version == 0 else []
        try:
            _apply(conn, attached)
        finally:
            for _, alias in attached:
                conn.execute(f"DETACH DATABASE {alias}")
    finally:
        conn.close()


def _apply(conn: sqlite3.Connection, attached: list[tuple[str, str]]) -> None:
    """Все миграции новее текущей версии — одной транзакцией; ошибка — откат целиком."""
    t0 = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _user_version(conn)  # другой процесс мог перенести, пока ждали замок
        for number, step in MIGRATIONS:
            if number > current:
                step(conn, attached)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if current < SCHEMA_VERSION:
        log.info("avia.sqlite: схема %s → %s за %.1f с", current, SCHEMA_VERSION, time.perf_counter() - t0)


def _legacy_fingerprint(state_dir: Path) -> dict[str, list[int]]:
    """{имя: [размер, счётчик изменений из заголовка SQLite]} старых файлов, что есть на диске.

    Счётчик (байты 24–27) растёт с каждой записью в режиме journal_mode=delete и не зависит от mtime:
    копия каталога cp без -p не выглядит изменённой.
    """
    out: dict[str, list[int]] = {}
    for name in LEGACY_DBS:
        path = state_dir / f"{name}.sqlite"
        try:
            with open(path, "rb") as f:
                header = f.read(28)
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        out[name] = [size, int.from_bytes(header[24:28], "big") if len(header) == 28 else -1]
    return out


def _check_legacy_unchanged(conn: sqlite3.Connection, target: Path) -> None:
    """Старый файл изменился после переноса — при DB_SINGLE=0 писали мимо avia.sqlite: не стартуем молча.

    Удалённые старые файлы — не ошибка: владелец может убрать их, когда DB_SINGLE=1 прижился.
    """
    row = conn.execute("SELECT value FROM db_meta WHERE key = 'legacy_files'").fetchone()
    if row is None:
        return
    before = json.loads(row[0])
    changed = sorted(n for n, fp in _legacy_fingerprint(target.parent).items() if before.get(n) != fp)
    if changed:
        files = ", ".join(f"{n}.sqlite" for n in changed)
        raise MigrationError(
            f"{files} изменились после переноса в {target.name}: бот, похоже, работал с DB_SINGLE=0 без "
            "python -m avia.cli db-rollback, и записи того времени есть только в старых файлах. "
            f"Либо вернуть DB_SINGLE=0, либо переименовать {target.name} (и -wal, -shm) — следующий старт "
            "с DB_SINGLE=1 перенесёт файлы заново, а записи, сделанные при DB_SINGLE=1, останутся в отложенной копии.")


def _attach_legacy(conn: sqlite3.Connection, state_dir: Path) -> list[tuple[str, str]]:
    """Старые файлы — только на чтение (mode=ro): перенос их не меняет."""
    attached = []
    for name in LEGACY_DBS:
        path = state_dir / f"{name}.sqlite"
        if not path.is_file() or path.stat().st_size == 0:
            continue
        journal = path.with_name(path.name + "-journal")
        if journal.exists() and journal.stat().st_size > 0:
            raise MigrationError(f"{path.name}: незавершённая транзакция ({journal.name}); "
                                 "запустите бота один раз с DB_SINGLE=0, потом снова с 1")
        alias = f"legacy_{name}"
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (path.resolve().as_uri() + "?mode=ro",))
        attached.append((name, alias))
    return attached


def _copy_table(conn: sqlite3.Connection, alias: str, name: str, sql: str) -> int:
    """Таблица со схемой старого файла (колонки КТ2 и ALTER владельца — в sql) и всеми строками."""
    conn.execute(sql)
    q = _quote(name)
    conn.execute(f"INSERT INTO main.{q} SELECT * FROM {alias}.{q}")
    src = conn.execute(f"SELECT COUNT(*) FROM {alias}.{q}").fetchone()[0]
    dst = conn.execute(f"SELECT COUNT(*) FROM main.{q}").fetchone()[0]
    if src != dst:
        raise MigrationError(f"{name}: перенесено {dst} строк из {src}")
    return int(dst)


def _copy_sequences(conn: sqlite3.Connection, src: str, dst: str, tables: set[str] | None = None) -> None:
    """sqlite_sequence: AUTOINCREMENT не выдаёт заново id удалённых строк и после переноса."""
    if not conn.execute(f"SELECT 1 FROM {src}.sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        return
    for name, seq in conn.execute(f"SELECT name, seq FROM {src}.sqlite_sequence").fetchall():
        if tables is not None and name not in tables:
            continue
        cur = conn.execute(f"UPDATE {dst}.sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (seq, name))
        if cur.rowcount == 0:
            conn.execute(f"INSERT INTO {dst}.sqlite_sequence (name, seq) VALUES (?, ?)", (name, seq))


def _schema_objects(conn: sqlite3.Connection, schema: str) -> list[tuple[str, str, str, str]]:
    """(type, name, tbl_name, sql): сначала таблицы, потом индексы, потом триггеры и представления."""
    return conn.execute(
        f"SELECT type, name, tbl_name, sql FROM {schema}.sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, rowid").fetchall()


def _import_group(conn: sqlite3.Connection, group: str, alias: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind, name, _tbl, sql in _schema_objects(conn, alias):
        if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name = ?", (name,)).fetchone():
            raise MigrationError(f"{group}.sqlite: {kind} {name} уже есть в {SINGLE_DB_NAME} "
                                 "(одно имя в двух старых базах)")
        if kind == "table":
            counts[name] = _copy_table(conn, alias, name, sql)
            conn.execute("INSERT INTO db_legacy_tables (name, grp, rows_imported) VALUES (?, ?, ?)",
                         (name, group, counts[name]))
        else:
            conn.execute(sql)
    _copy_sequences(conn, alias, "main")
    return counts


def _m1_import_legacy(conn: sqlite3.Connection, attached: list[tuple[str, str]]) -> None:
    """Версия 1: служебные таблицы и перенос четырёх старых файлов."""
    conn.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE db_legacy_tables (name TEXT PRIMARY KEY, grp TEXT NOT NULL, "
                 "rows_imported INTEGER NOT NULL)")
    files = {group: _import_group(conn, group, alias) for group, alias in attached}
    conn.execute("INSERT INTO db_meta (key, value) VALUES ('legacy_import', ?)",
                 (json.dumps({"at": time.time(), "files": files}, ensure_ascii=False, sort_keys=True),))
    state_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    conn.execute("INSERT INTO db_meta (key, value) VALUES ('legacy_files', ?)",
                 (json.dumps(_legacy_fingerprint(state_dir), sort_keys=True),))
    log.info("avia.sqlite: перенос из %s", ", ".join(f"{g}.sqlite ({sum(c.values())} строк)"
                                                    for g, c in files.items()) or "— (старых файлов нет)")


def _m2_chat_state(conn: sqlite3.Connection, attached: list[tuple[str, str]]) -> None:
    """Версия 2: сессии диалога (JSON) и последние выдачи (JSON в zlib) — chat_state.py, одна строка на чат."""
    conn.execute("CREATE TABLE chat_sessions (chat_key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                 "updated_at REAL NOT NULL)")
    conn.execute("CREATE TABLE chat_snapshots (chat_key TEXT PRIMARY KEY, data BLOB NOT NULL, "
                 "created_at REAL NOT NULL, updated_at REAL NOT NULL)")


MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection, list[tuple[str, str]]], None]], ...] = (
    (1, _m1_import_legacy),
    (2, _m2_chat_state),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


# --- Откат: avia.sqlite → четыре файла -----------------------------------------------------------------------

def _schema_groups() -> dict[str, str]:
    """{таблица: старый файл} по SCHEMA сторов владельца: таблицы, созданные уже в avia.sqlite."""
    from . import history, metrics, subscriptions, users

    out: dict[str, str] = {}
    for group, module in (("users", users), ("subscriptions", subscriptions), ("history", history),
                          ("metrics", metrics)):
        for name in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", module.SCHEMA):
            out[name] = group
    return out


def _owner_tables_by_group(src: Path) -> dict[str, list[str]]:
    with closing(connect(src)) as conn:
        recorded = dict(conn.execute("SELECT name, grp FROM db_legacy_tables").fetchall())
        tables = _main_tables(conn) - KT4_TABLES
        views = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")]
    if views:
        raise MigrationError(f"в {SINGLE_DB_NAME} представления {', '.join(views)}: выгрузка их не переносит, "
                             "остановлена, файлы не тронуты")
    known = _schema_groups()
    groups: dict[str, list[str]] = {g: [] for g in LEGACY_DBS}
    unknown = []
    for name in sorted(tables):
        group = recorded.get(name) or known.get(name)
        if group is None:
            unknown.append(name)
        else:
            groups[group].append(name)
    if unknown:
        raise MigrationError(f"в {SINGLE_DB_NAME} таблицы без старого файла: {', '.join(unknown)}; "
                             "выгрузка остановлена, файлы не тронуты")
    return groups


def _export_group(src: Path, tmp: Path, tables: list[str]) -> dict[str, int]:
    """Новый файл tmp: таблицы tables со схемой и строками из avia.sqlite, их индексы, триггеры, sqlite_sequence."""
    conn = connect(tmp, isolation_level=None)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src),))
        try:
            conn.execute("BEGIN")
            wanted = set(tables)
            counts = {}
            for kind, name, tbl, sql in _schema_objects(conn, "src"):
                if kind == "table" and name in wanted:
                    counts[name] = _copy_table(conn, "src", name, sql)
                elif kind != "table" and tbl in wanted:
                    conn.execute(sql)
            _copy_sequences(conn, "src", "main", wanted)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("DETACH DATABASE src")
    finally:
        conn.close()
    return counts


_SIDECARS = ("", "-journal", "-wal", "-shm")


def _set_aside(path: Path, tag: str) -> None:
    """path и его -journal, -wal, -shm — в path.<tag>…: не удаляем, откладываем."""
    for suffix in _SIDECARS:
        p = path.with_name(path.name + suffix)
        if p.exists():
            os.replace(p, path.with_name(f"{path.name}.{tag}{suffix}"))


def _free_stamp(state_dir: Path) -> str:
    """Метка времени, под которой ещё нет отложенных файлов: два отката в одну секунду не затирают друг друга."""
    base = time.strftime("%Y%m%d-%H%M%S")
    names = [f"{g}.sqlite.pre-rollback-" for g in LEGACY_DBS] + [f"{SINGLE_DB_NAME}.rolledback-"]
    for n in range(1000):
        stamp = base if n == 0 else f"{base}-{n}"
        if not any((state_dir / f"{name}{stamp}{sfx}").exists() for name in names for sfx in _SIDECARS):
            return stamp
    raise MigrationError("нет свободного имени для отложенных файлов")


def export_legacy(storage_dir: Path | str) -> dict[str, dict[str, int]]:
    """Откат: всё из avia.sqlite — в users, subscriptions, history, metrics.sqlite; {файл: {таблица: строк}}.

    Бот должен быть остановлен. Прежние четыре файла откладываются в <имя>.sqlite.pre-rollback-<время>,
    avia.sqlite — в avia.sqlite.rolledback-<время>: следующий DB_SINGLE=1 перенесёт файлы заново.
    """
    state_dir = Path(storage_dir)
    src = single_db_path(state_dir)
    if not src.is_file():
        raise MigrationError(f"нет {src}: выгружать нечего")
    groups = _owner_tables_by_group(src)
    stamp = _free_stamp(state_dir)
    tmps: dict[str, Path] = {}
    report: dict[str, dict[str, int]] = {}
    try:
        for group in LEGACY_DBS:
            tmp = state_dir / f".{group}.sqlite.export-{stamp}.tmp"
            tmps[group] = tmp
            report[group] = _export_group(src, tmp, groups[group])
    except BaseException:
        for tmp in tmps.values():
            tmp.unlink(missing_ok=True)
        raise
    for group, tmp in tmps.items():
        target = state_dir / f"{group}.sqlite"
        _set_aside(target, f"pre-rollback-{stamp}")
        os.replace(tmp, target)
    with closing(connect(src)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _set_aside(src, f"rolledback-{stamp}")
    with _LOCK:
        _READY.discard(os.path.abspath(src))
    log.info("avia.sqlite выгружена в четыре файла: %s", report)
    return report
