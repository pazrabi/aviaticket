"""КТ4, работа 1: перенос четырёх старых баз в одну state/avia.sqlite (DB_SINGLE=1, ADR-002).

Перенос — один раз, в одной транзакции; старые файлы не меняются; повторный старт ничего не переносит.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from avia import statedb
from avia.metrics import MetricsStore
from avia.models import Query
from avia.criteria import SearchCriteria
from avia.subscriptions import SubscriptionStore
from avia.users import UserStore
from tests.legacy_db_fixtures import (
    KT2_COLUMNS,
    LEGACY,
    PEOPLE,
    build_legacy_state,
    legacy_contents,
    sequences,
    table_rows,
    user_tables,
)

KT4_TABLES = {"db_meta", "db_legacy_tables", "chat_sessions", "chat_snapshots"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    d = tmp_path / "state"
    build_legacy_state(d)
    return d


def file_digests(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir()) if p.is_file()}


def test_four_files_into_one_all_rows_in_place(state):
    before = legacy_contents(state)
    seq_before = {}
    for name in LEGACY:
        seq_before.update(sequences(state / f"{name}.sqlite"))

    path = statedb.ensure_single_db(state)

    assert path == state / "avia.sqlite"
    assert set(user_tables(path)) == set(before) | KT4_TABLES
    for table, rows in before.items():
        assert len(rows) > 0, table
        assert table_rows(path, table) == rows, table  # счёт строк и строки целиком, по каждой таблице
    assert sequences(path) == seq_before


def test_wal_version_and_busy_timeout(state):
    path = statedb.ensure_single_db(state)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == statedb.SCHEMA_VERSION
    with statedb.connect(path) as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == statedb.BUSY_TIMEOUT_MS
    # сторы владельца открывают ту же базу с timeout=10: это и есть busy_timeout 10 000 мс
    with UserStore(path)._connect() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000


def test_old_files_untouched(state):
    before = file_digests(state)
    statedb.ensure_single_db(state)
    statedb.forget_ready()
    statedb.ensure_single_db(state)
    after = {k: v for k, v in file_digests(state).items() if not k.startswith("avia.sqlite")}
    assert after == before


def test_second_start_imports_nothing(state):
    path = statedb.ensure_single_db(state)
    UserStore(path).register(999, username="после переноса")
    counts = {t: len(table_rows(path, t)) for t in user_tables(path)}

    statedb.forget_ready()
    statedb.ensure_single_db(state)
    statedb.forget_ready()
    statedb.ensure_single_db(state)

    assert {t: len(table_rows(path, t)) for t in user_tables(path)} == counts
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM db_meta WHERE key = 'legacy_import'").fetchone()[0] == 1


def test_stores_read_migrated_data(state):
    path = statedb.ensure_single_db(state)
    users = UserStore(path)
    assert {u for u in PEOPLE if users.get(u) is not None} == set(PEOPLE)
    assert users.get(PEOPLE[2]).blocked is True
    subs = SubscriptionStore(path)
    assert [s.id for s in subs.list_active(PEOPLE[0])] == [1]
    # верхний id был удалён до переноса: sqlite_sequence перенесён, id не повторяется
    q = Query(origin="LED", destination="KZN", depart=subs.get(1).depart)
    assert subs.create(PEOPLE[0], q, "any", SearchCriteria()).id == 4


def test_kt2_columns_carried_over(state):
    path = statedb.ensure_single_db(state)
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(searches)")}
    assert set(KT2_COLUMNS) <= cols
    assert table_rows(path, "searches") == table_rows(state / "metrics.sqlite", "searches")


def test_metrics_before_kt2_gets_columns_from_owner_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    d = tmp_path / "state"
    build_legacy_state(d, kt2_columns=False)
    old_rows = table_rows(d / "metrics.sqlite", "searches")

    path = statedb.ensure_single_db(d)
    MetricsStore(path)._connect().close()  # ALTER владельца добавляет card_* и preview_* уже в avia.sqlite

    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(searches)")}
        n = db.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    assert set(KT2_COLUMNS) <= cols
    assert n == len(old_rows)


def test_failure_midway_leaves_nothing_and_next_start_imports(state, monkeypatch):
    real = statedb._copy_table
    calls = []

    def flaky(conn, alias, name, sql):
        calls.append(name)
        if len(calls) == 5:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, alias, name, sql)

    monkeypatch.setattr(statedb, "_copy_table", flaky)
    with pytest.raises(sqlite3.OperationalError):
        statedb.ensure_single_db(state)
    path = state / "avia.sqlite"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert user_tables(path) == []

    monkeypatch.setattr(statedb, "_copy_table", real)
    statedb.ensure_single_db(state)
    for table, rows in legacy_contents(state).items():
        assert table_rows(path, table) == rows, table


def test_missing_legacy_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    d = tmp_path / "state"
    d.mkdir()
    UserStore(d / "users.sqlite").register(7)

    path = statedb.ensure_single_db(d)
    assert UserStore(path).get(7) is not None
    assert SubscriptionStore(path).list_active(7) == []  # таблицы подписок создаёт стор владельца


def test_fresh_install_without_old_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    path = statedb.ensure_single_db(tmp_path / "new_state")
    assert set(user_tables(path)) == KT4_TABLES


def test_name_collision_stops_migration(state):
    with sqlite3.connect(state / "history.sqlite") as db:
        db.execute("CREATE TABLE users (x INTEGER)")
    with pytest.raises(statedb.MigrationError, match="users"):
        statedb.ensure_single_db(state)
    assert user_tables(state / "avia.sqlite") == []


def test_unversioned_db_with_tables_is_not_overwritten(state):
    with sqlite3.connect(state / "avia.sqlite") as db:
        db.execute("CREATE TABLE something (x INTEGER)")
        db.execute("INSERT INTO something VALUES (1)")
    with pytest.raises(statedb.MigrationError, match="без номера версии"):
        statedb.ensure_single_db(state)
    assert table_rows(state / "avia.sqlite", "something") == [(1,)]


def test_newer_schema_refused(state):
    path = statedb.ensure_single_db(state)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {statedb.SCHEMA_VERSION + 1}")
    statedb.forget_ready()
    with pytest.raises(statedb.MigrationError, match="новее"):
        statedb.ensure_single_db(state)


def test_db_path_helper_follows_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "0")
    statedb.forget_ready()
    for name in LEGACY:
        assert statedb.db_path(tmp_path, name) == tmp_path / f"{name}.sqlite"
    assert not (tmp_path / "avia.sqlite").exists()

    monkeypatch.setenv("DB_SINGLE", "1")
    for name in LEGACY:
        assert statedb.db_path(tmp_path, name) == tmp_path / "avia.sqlite"
    with pytest.raises(ValueError):
        statedb.db_path(tmp_path, "phones")
