"""Тесты системы бэкапов SQLite баз и ежедневного расписания в Bot."""
from __future__ import annotations

from datetime import datetime
import gzip
import os
from pathlib import Path
import sqlite3

import pytest

from avia.backup import backup_all, list_backups
from avia.bot import Bot
from avia.config import Settings


def _create_sample_db(path: Path, table_name: str, rows_count: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, value TEXT)")
        for i in range(rows_count):
            db.execute(f"INSERT INTO {table_name} (value) VALUES (?)", (f"val_{i}",))


def test_backup_all_creates_valid_gzip_and_integrity(tmp_path):
    """backup_all делает консистентную копию через Connection.backup(), сжимает gzip и проверяет integrity."""
    state_dir = tmp_path / "state"
    dest_dir = tmp_path / "backups"

    _create_sample_db(state_dir / "history.sqlite", "price_history", 3)
    _create_sample_db(state_dir / "users.sqlite", "users", 2)

    now_dt = datetime(2026, 11, 4, 14, 30)
    created = backup_all(state_dir, dest_dir, keep=14, now=now_dt)

    assert len(created) == 2
    filenames = {p.name for p in created}
    assert "history-20261104-1430.sqlite.gz" in filenames
    assert "users-20261104-1430.sqlite.gz" in filenames

    # Проверяем распаковку и integrity_check
    hist_gz = dest_dir / "history-20261104-1430.sqlite.gz"
    decompressed_db = tmp_path / "restored_hist.sqlite"
    with gzip.open(hist_gz, "rb") as f_in, open(decompressed_db, "wb") as f_out:
        f_out.write(f_in.read())

    with sqlite3.connect(decompressed_db) as db:
        res = db.execute("PRAGMA integrity_check").fetchone()
        assert res[0] == "ok"
        cnt = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        assert cnt == 3


def test_backup_skips_corrupted_integrity(tmp_path):
    """База с нарушенной структурой пропускается с записью в лог и не создаёт бэкап."""
    state_dir = tmp_path / "state"
    dest_dir = tmp_path / "backups"
    state_dir.mkdir(parents=True, exist_ok=True)

    bad_file = state_dir / "corrupted.sqlite"
    bad_file.write_bytes(b"SQLite format 3\x00this is garbage corrupt data")

    created = backup_all(state_dir, dest_dir)
    assert len(created) == 0
    assert len(list(dest_dir.glob("*.sqlite.gz"))) == 0


def test_backup_ignores_backups_and_profiles_dirs(tmp_path):
    """Игнорируются подкаталоги backups и profiles в state_dir."""
    state_dir = tmp_path / "state"
    dest_dir = state_dir / "backups"  # dest_dir внутри state_dir!

    # Создаём базы в корне, в backups/ и в profiles/
    _create_sample_db(state_dir / "history.sqlite", "history", 1)
    _create_sample_db(state_dir / "backups" / "old.sqlite", "old", 1)
    _create_sample_db(state_dir / "profiles" / "site" / "cookies.sqlite", "cookies", 1)

    created = backup_all(state_dir, dest_dir, keep=14)
    # Только история из корня state_dir должна быть забэкаплена
    assert len(created) == 1
    assert created[0].name.startswith("history-")


def test_backup_retention_keep(tmp_path):
    """Удаляются копии сверх keep штук, остаётся ровно keep самых свежих."""
    state_dir = tmp_path / "state"
    dest_dir = tmp_path / "backups"
    _create_sample_db(state_dir / "history.sqlite", "history", 1)

    # Создаём 5 бэкапов последовательно
    for hour in range(5):
        dt = datetime(2026, 11, 4, 10 + hour, 0)
        backup_all(state_dir, dest_dir, keep=3, now=dt)

    remaining = sorted(f.name for f in dest_dir.glob("history-*.sqlite.gz"))
    assert len(remaining) == 3
    # Должны остаться часы 12, 13, 14
    assert remaining == [
        "history-20261104-1200.sqlite.gz",
        "history-20261104-1300.sqlite.gz",
        "history-20261104-1400.sqlite.gz",
    ]


def test_backup_same_minute_does_not_delete_other_files(tmp_path):
    """Повторный вызов в ту же минуту не затирает и не удаляет бэкапы других баз."""
    state_dir = tmp_path / "state"
    dest_dir = tmp_path / "backups"

    _create_sample_db(state_dir / "history.sqlite", "history", 1)
    _create_sample_db(state_dir / "users.sqlite", "users", 1)

    dt = datetime(2026, 11, 4, 12, 0)
    # Первый запуск
    backup_all(state_dir, dest_dir, keep=14, now=dt)
    # Второй запуск в ту же самую минуту
    backup_all(state_dir, dest_dir, keep=14, now=dt)

    files = {f.name for f in dest_dir.glob("*.sqlite.gz")}
    assert "history-20261104-1200.sqlite.gz" in files
    assert "users-20261104-1200.sqlite.gz" in files
    assert len(files) == 2


def test_list_backups(tmp_path):
    """list_backups возвращает список бэкапов по убыванию времени со всеми нужными ключами."""
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)

    f1 = dest_dir / "history-20261104-1000.sqlite.gz"
    f1.write_bytes(b"data1")
    os.utime(f1, (1000.0, 1000.0))

    f2 = dest_dir / "users-20261104-1100.sqlite.gz"
    f2.write_bytes(b"data222")
    os.utime(f2, (2000.0, 2000.0))

    backups = list_backups(dest_dir)
    assert len(backups) == 2

    # f2 свежее, поэтому идёт первым
    assert backups[0]["name"] == "users-20261104-1100.sqlite.gz"
    assert backups[0]["db"] == "users"
    assert backups[0]["size"] == 7
    assert backups[0]["created_at"] == 2000.0
    # Проверка русских ключей-алиасов
    assert backups[0]["база"] == "users"
    assert backups[0]["имя"] == "users-20261104-1100.sqlite.gz"
    assert backups[0]["размер"] == 7
    assert backups[0]["время создания"] == 2000.0

    assert backups[1]["name"] == "history-20261104-1000.sqlite.gz"
    assert backups[1]["db"] == "history"


def test_daily_backup_timing_and_restart(tmp_path):
    """Логика ежедневного бэкапа: до backup_hour не запускается, после запускается 1 раз, переживает рестарт."""
    storage = tmp_path / "storage"
    backup_dir = storage / "backups"
    storage.mkdir(parents=True, exist_ok=True)
    _create_sample_db(storage / "history.sqlite", "history", 1)

    settings = Settings(
        sites=[],
        tg_token="dummy_token_123",
        tg_allowed_ids={1},
        storage_dir=storage,
        backup_dir=backup_dir,
        backup_hour=4,
    )

    bot = Bot(settings)

    # 1. До 04:00 (например в 03:50) бэкап не должен запускаться
    t_0350 = datetime(2026, 11, 4, 3, 50)
    assert bot._should_run_daily_backup(now=t_0350) is False

    # 2. В 04:05 бэкапов сегодня ещё не было -> должен запуститься
    t_0405 = datetime(2026, 11, 4, 4, 5)
    assert bot._should_run_daily_backup(now=t_0405) is True

    # 3. Делаем бэкап (имитируя время 04:05)
    created = backup_all(storage, backup_dir, keep=14, now=t_0405)
    assert len(created) > 0

    # 4. В 04:10 в этот же день бэкап больше не должен запускаться
    t_0410 = datetime(2026, 11, 4, 4, 10)
    assert bot._should_run_daily_backup(now=t_0410) is False

    # 5. Перезапуск бота позже в тот же день (15:00)
    restarted_bot = Bot(settings)
    assert restarted_bot.last_backup_at is not None
    assert restarted_bot._should_run_daily_backup(now=datetime(2026, 11, 4, 15, 0)) is False

    # 6. На следующий день в 04:01 бэкап снова должен запуститься
    t_next_day = datetime(2026, 11, 5, 4, 1)
    assert restarted_bot._should_run_daily_backup(now=t_next_day) is True


def test_bot_backup_now_and_last_backup_at(tmp_path):
    """Bot.backup_now() создаёт бэкапы и обновляет self.last_backup_at."""
    storage = tmp_path / "storage"
    backup_dir = storage / "backups"
    storage.mkdir(parents=True, exist_ok=True)
    _create_sample_db(storage / "history.sqlite", "history", 1)

    settings = Settings(
        sites=[],
        tg_token="dummy_token_123",
        tg_allowed_ids={1},
        storage_dir=storage,
        backup_dir=backup_dir,
    )
    bot = Bot(settings)
    assert bot.last_backup_at is None
    assert bot.started_at > 0

    created = bot.backup_now()
    assert len(created) == 1
    assert bot.last_backup_at is not None
    assert bot.last_backup_at > 0


def test_backup_works_when_state_dir_path_contains_profiles_or_backups(tmp_path):
    """Имена каталогов «profiles»/«backups» в пути к state не должны отключать бэкап."""
    import sqlite3

    from avia.backup import backup_all

    state = tmp_path / "profiles" / "backups" / "state"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "users.sqlite") as db:
        db.execute("create table t (x)")
        db.execute("insert into t values (1)")
    made = backup_all(state, tmp_path / "out", keep=3)
    assert len(made) == 1 and made[0].name.startswith("users-")
