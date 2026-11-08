"""КТ4, работа 1: откат без потерь — из avia.sqlite обратно в четыре файла (avia.cli db-rollback).

Перенос → новые записи → выгрузка → код при DB_SINGLE=0 видит всё, что было записано после переноса.
"""
from __future__ import annotations

import sqlite3
import sys

import pytest

from avia import statedb
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.history import PriceHistory
from avia.metrics import MetricsStore
from avia.models import Query
from avia.subscriptions import SubscriptionStore
from avia.users import UserStore
from tests.legacy_db_fixtures import LEGACY, PEOPLE, build_legacy_state, fill_generic, table_rows, user_tables

NEW_PERSON = 777


def write_after_migration(path) -> None:
    """Всё, что бот пишет после переноса: новые строки, правки старых, удаление."""
    users = UserStore(path)
    users.register(NEW_PERSON, username="новый")
    users.register(PEOPLE[0], username="переименован")  # правка строки, перенесённой из старого файла
    subs = SubscriptionStore(path)
    old = subs.list_active(PEOPLE[0])[0]
    new = subs.create(NEW_PERSON, Query("MOW", "KZN", old.depart), "with", SearchCriteria(max_transfers=0))
    subs.record_price(new.id, 4200, "RUB", "o-new", 1_790_100_000.0)
    subs.deactivate(old.id, PEOPLE[0])
    PriceHistory(path)
    fill_generic(path, "price_history", n=3)
    metrics = MetricsStore(path)
    metrics.record_run("user", "site9", True, False, False, None, 5, 12.5, ts=1_790_100_000.0)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM attempts WHERE id = (SELECT MIN(id) FROM attempts)")
        db.execute("INSERT INTO chat_sessions (chat_key, data, updated_at) VALUES ('777', '{}', 1)")


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    d = tmp_path / "state"
    build_legacy_state(d)
    path = statedb.ensure_single_db(d)
    write_after_migration(path)
    return d


def contents(path, tables):
    return {t: table_rows(path, t) for t in tables}


def test_export_then_flag_off_sees_everything(migrated, monkeypatch):
    avia_path = migrated / "avia.sqlite"
    owner_tables = [t for t in user_tables(avia_path) if t not in statedb.KT4_TABLES]
    expected = contents(avia_path, owner_tables)

    report = statedb.export_legacy(migrated)

    monkeypatch.setenv("DB_SINGLE", "0")
    statedb.forget_ready()
    got = {}
    for name in LEGACY:
        got.update(contents(statedb.db_path(migrated, name), user_tables(migrated / f"{name}.sqlite")))
    assert got == expected  # каждая таблица — строка в строку
    assert sum(n for tables in report.values() for n in tables.values()) == sum(map(len, expected.values()))
    # код владельца при DB_SINGLE=0 видит записи после переноса
    users = UserStore(statedb.db_path(migrated, "users"))
    assert users.get(NEW_PERSON).username == "новый"
    assert users.get(PEOPLE[0]).username == "переименован"
    subs = SubscriptionStore(statedb.db_path(migrated, "subscriptions"))
    assert [s.destination for s in subs.list_active(NEW_PERSON)] == ["KZN"]
    assert subs.list_active(PEOPLE[0]) == []
    assert subs.first_price(subs.list_active(NEW_PERSON)[0].id) == 4200
    with sqlite3.connect(statedb.db_path(migrated, "metrics")) as db:
        assert db.execute("SELECT COUNT(*) FROM site_runs WHERE site = 'site9'").fetchone()[0] == 1
    # таблицы КТ4 в старые файлы не едут: при 0 сессии в памяти
    for name in LEGACY:
        assert not set(user_tables(migrated / f"{name}.sqlite")) & statedb.KT4_TABLES


def test_export_keeps_old_files_and_retires_single_db(migrated):
    statedb.export_legacy(migrated)
    names = {p.name for p in migrated.iterdir()}
    assert "avia.sqlite" not in names
    assert any(n.startswith("avia.sqlite.rolledback-") for n in names)
    for name in LEGACY:
        assert any(n.startswith(f"{name}.sqlite.pre-rollback-") for n in names), name
    assert not any(n.endswith(".tmp") for n in names)


def test_sequence_survives_round_trip(migrated, monkeypatch):
    statedb.export_legacy(migrated)
    monkeypatch.setenv("DB_SINGLE", "0")
    subs = SubscriptionStore(migrated / "subscriptions.sqlite")
    q = Query("LED", "AER", subs.list_active(NEW_PERSON)[0].depart)
    assert subs.create(5, q, "any", SearchCriteria()).id == 5  # 1–3 до переноса, 4 после, удалённый 3 не вернулся


def test_flag_on_again_imports_fresh_files(migrated, monkeypatch):
    statedb.export_legacy(migrated)
    monkeypatch.setenv("DB_SINGLE", "0")
    statedb.forget_ready()
    UserStore(statedb.db_path(migrated, "users")).register(888, username="при нуле")

    monkeypatch.setenv("DB_SINGLE", "1")
    users = UserStore(statedb.db_path(migrated, "users"))
    assert users.get(888).username == "при нуле"
    assert users.get(NEW_PERSON) is not None


def test_unknown_table_stops_export(migrated):
    with sqlite3.connect(migrated / "avia.sqlite") as db:
        db.execute("CREATE TABLE mystery (x INTEGER)")
    with pytest.raises(statedb.MigrationError, match="mystery"):
        statedb.export_legacy(migrated)
    assert (migrated / "avia.sqlite").exists()
    assert not any(p.name.endswith(".tmp") for p in migrated.iterdir())


def test_cli_db_rollback(migrated, monkeypatch, capsys):
    import avia.cli

    monkeypatch.setattr(avia.cli, "load_settings", lambda: Settings(sites=[], storage_dir=migrated))
    monkeypatch.setattr(sys, "argv", ["avia.cli", "db-rollback"])
    avia.cli.main()
    out = capsys.readouterr().out
    assert "users.sqlite" in out and "DB_SINGLE=0" in out
    assert not (migrated / "avia.sqlite").exists()
    assert UserStore(migrated / "users.sqlite").get(NEW_PERSON) is not None


def test_cli_db_rollback_without_single_db(tmp_path, monkeypatch):
    import avia.cli

    monkeypatch.setattr(avia.cli, "load_settings", lambda: Settings(sites=[], storage_dir=tmp_path))
    monkeypatch.setattr(sys, "argv", ["avia.cli", "db-rollback"])
    with pytest.raises(SystemExit, match="avia.sqlite"):
        avia.cli.main()


def test_two_rollbacks_in_one_second_keep_both_copies(migrated, monkeypatch):
    monkeypatch.setattr(statedb.time, "strftime", lambda fmt, *a: "20261108-210000")
    statedb.export_legacy(migrated)
    statedb.ensure_single_db(migrated)  # снова DB_SINGLE=1: перенос из выгруженных файлов
    statedb.export_legacy(migrated)
    names = {p.name for p in migrated.iterdir()}
    assert {"avia.sqlite.rolledback-20261108-210000", "avia.sqlite.rolledback-20261108-210000-1"} <= names
    assert {"users.sqlite.pre-rollback-20261108-210000", "users.sqlite.pre-rollback-20261108-210000-1"} <= names


def test_view_stops_export(migrated):
    with sqlite3.connect(migrated / "avia.sqlite") as db:
        db.execute("CREATE VIEW active_people AS SELECT tg_id FROM users")
    with pytest.raises(statedb.MigrationError, match="active_people"):
        statedb.export_legacy(migrated)
    assert (migrated / "avia.sqlite").exists()
