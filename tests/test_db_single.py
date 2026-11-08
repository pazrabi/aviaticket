"""КТ4, работа 1: все места берут путь базы из statedb.db_path; бэкап avia.sqlite вместе с WAL и восстановление."""
from __future__ import annotations

import gzip
from pathlib import Path
import re
import shutil
import sqlite3

import pytest

from avia import statedb
from avia.backup import backup_all
from avia.config import Settings
from avia.users import UserStore
from tests.legacy_db_fixtures import build_legacy_state, table_rows, user_tables

SRC = Path(__file__).resolve().parents[1] / "src" / "avia"
OLD_NAMES = re.compile(r"""["'](users|subscriptions|history|metrics)\.sqlite["']""")


def test_no_module_builds_old_file_names_itself():
    """Имена четырёх файлов — только в statedb: остальные берут путь из db_path."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "statedb.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if OLD_NAMES.search(code) and '"""' not in code:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert offenders == []


@pytest.fixture
def single(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    d = tmp_path / "state"
    build_legacy_state(d)
    return d


@pytest.mark.parametrize("flag, expected", [("0", "metrics.sqlite"), ("1", "avia.sqlite")])
def test_metrics_readers_follow_flag(tmp_path, monkeypatch, flag, expected):
    from avia import dispatcher, speed

    monkeypatch.setenv("DB_SINGLE", flag)
    statedb.forget_ready()
    d = tmp_path / "state"
    build_legacy_state(d)
    s = Settings(sites=[], storage_dir=d)
    assert speed.SpeedTable(settings=s).metrics_path == d / expected
    assert speed.get_speed_table(s).metrics_path == d / expected
    assert dispatcher._get_metrics(s).path == d / expected


def test_phone_check_reference_reads_single_db(single):
    from datetime import date, timedelta
    import time

    from avia import phone_check
    from avia.models import Query

    path = statedb.ensure_single_db(single)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO site_runs (ts, source, site, ok, partial, captcha, offers, elapsed, min_price) "
                   "VALUES (?, 'phone_check', 'aviasales', 1, 0, 0, 3, 1.0, 4321)", (time.time(),))
    (single / "metrics.sqlite").unlink()  # старые файлы больше не источник
    (single / "history.sqlite").unlink()
    q = Query("MOW", "AER", date.today() + timedelta(days=20))
    assert phone_check.get_reference_price(Settings(sites=[], storage_dir=single), "aviasales", q) == 4321.0


def test_backup_takes_single_db_with_wal_and_restores(single, tmp_path):
    path = statedb.ensure_single_db(single)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")  # строки остаются только в avia.sqlite-wal
    UserStore(path)  # схема
    writer.executemany("INSERT INTO users (tg_id, created_at, last_seen_at) VALUES (?, 1, 1)",
                       [(5000 + i,) for i in range(50)])
    writer.commit()
    assert (single / "avia.sqlite-wal").stat().st_size > 0
    main_only = tmp_path / "main_only"
    main_only.mkdir()
    shutil.copy2(path, main_only / "avia.sqlite")  # без -wal новых строк нет: WAL обязателен в копии
    with sqlite3.connect(main_only / "avia.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE tg_id >= 5000").fetchone()[0] == 0

    created = backup_all(single, tmp_path / "backups", keep=3)
    writer.close()

    assert [p.name.split("-")[0] for p in created] == ["avia"]  # старые четыре файла не копируются
    restored = tmp_path / "restored_state"
    restored.mkdir()
    with gzip.open(created[0], "rb") as src, open(restored / "avia.sqlite", "wb") as dst:
        shutil.copyfileobj(src, dst)
    statedb.forget_ready()
    restored_path = statedb.ensure_single_db(restored)  # номер версии есть — ничего не переносит
    with sqlite3.connect(restored_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM users WHERE tg_id >= 5000").fetchone()[0] == 50
    for table in user_tables(path):
        assert table_rows(restored_path, table) == table_rows(path, table), table
    assert UserStore(restored_path).get(5049) is not None


def test_backup_flag_off_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "0")
    d = tmp_path / "state"
    build_legacy_state(d)
    created = backup_all(d, tmp_path / "backups", keep=3)
    assert sorted(p.name.split("-")[0] for p in created) == ["history", "metrics", "subscriptions", "users"]
