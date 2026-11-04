"""Ежедневные бэкапы баз данных SQLite и управление архивами."""
from __future__ import annotations

from datetime import datetime
import gzip
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any

log = logging.getLogger("avia.backup")


def _is_db_backup_file(path: Path, base_name: str) -> bool:
    """Проверяет, что архивный файл относится именно к базе base_name."""
    if not (path.is_file() and path.name.endswith(".sqlite.gz")):
        return False
    # Имя файла имеет формат <base_name>-YYYYMMDD-HHMM.sqlite.gz
    m = re.match(r"^(.+)-(\d{8}-\d{4}.*)\.sqlite\.gz$", path.name)
    if not m:
        return False
    return m.group(1) == base_name


def backup_all(
    state_dir: Path | str,
    dest_dir: Path | str,
    keep: int = 14,
    now: float | datetime | None = None,
) -> list[Path]:
    """Создаёт консистентные сжатые копии всех баз *.sqlite из state_dir в dest_dir."""
    state_path = Path(state_dir)
    dest_path = Path(dest_dir)

    if not state_path.exists():
        return []

    dest_path.mkdir(parents=True, exist_ok=True)

    if now is None:
        dt = datetime.now()
    elif isinstance(now, (int, float)):
        dt = datetime.fromtimestamp(now)
    else:
        dt = now

    ts_str = dt.strftime("%Y%m%d-%H%M")
    created: list[Path] = []

    # Находим все *.sqlite файлы в корне каталога state_path (игнорируя backups и profiles)
    sqlite_files = [
        f for f in sorted(state_path.iterdir())
        if f.is_file() and f.suffix == ".sqlite" and not f.name.startswith(".")
        and "backups" not in f.parts and "profiles" not in f.parts
    ]

    for src_file in sqlite_files:
        base_name = src_file.stem
        # 1. Создание временной копии через sqlite3 Connection.backup()
        tmp_fd, tmp_file_name = tempfile.mkstemp(suffix=".sqlite")
        os.close(tmp_fd)
        tmp_path = Path(tmp_file_name)

        try:
            with sqlite3.connect(src_file) as src_conn:
                with sqlite3.connect(tmp_path) as dst_conn:
                    src_conn.backup(dst_conn)

            # 2. Проверка целостности PRAGMA integrity_check == 'ok'
            with sqlite3.connect(tmp_path) as chk_conn:
                row = chk_conn.execute("PRAGMA integrity_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    log.error("Целостность базы %s нарушена (integrity_check=%s), бэкап пропущен", src_file.name, row)
                    continue

            # 3. Сжатие в dest_dir/<имя>-YYYYMMDD-HHMM.sqlite.gz
            target_name = f"{base_name}-{ts_str}.sqlite.gz"
            target_file = dest_path / target_name
            tmp_gz = dest_path / f".{target_name}.tmp"

            with open(tmp_path, "rb") as f_in:
                with gzip.open(tmp_gz, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            tmp_gz.replace(target_file)

            if now is not None:
                mtime = dt.timestamp()
                os.utime(target_file, (mtime, mtime))

            created.append(target_file)

            # 4. Удаление старых копий этой же базы сверх keep штук
            all_db_backups = [f for f in dest_path.iterdir() if _is_db_backup_file(f, base_name)]
            # Сортируем самые свежие первыми (по mtime, затем по имени)
            all_db_backups.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)

            if len(all_db_backups) > keep:
                for old_f in all_db_backups[keep:]:
                    try:
                        old_f.unlink()
                    except OSError as e:
                        log.warning("Не удалось удалить старый бэкап %s: %s", old_f, e)

        except Exception:
            log.exception("ошибка создания бэкапа для базы %s", src_file.name)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    return created


def list_backups(dest_dir: Path | str) -> list[dict[str, Any]]:
    """Возвращает список бэкапов в dest_dir по убыванию времени создания."""
    dest_path = Path(dest_dir)
    if not dest_path.exists():
        return []

    items: list[dict[str, Any]] = []
    for f in dest_path.iterdir():
        if not f.is_file() or not f.name.endswith(".sqlite.gz") or f.name.startswith("."):
            continue
        m = re.match(r"^(.+)-(\d{8}-\d{4}.*)\.sqlite\.gz$", f.name)
        db_name = m.group(1) if m else f.name.split("-")[0]
        st = f.stat()
        items.append({
            "name": f.name,
            "db": db_name,
            "size": st.st_size,
            "created_at": st.st_mtime,
            "имя": f.name,
            "база": db_name,
            "размер": st.st_size,
            "время создания": st.st_mtime,
        })

    items.sort(key=lambda x: (x["created_at"], x["name"]), reverse=True)
    return items
