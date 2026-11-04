from __future__ import annotations

import time
import pytest

from avia.users import UserStore


def test_user_store_refund_search(tmp_path):
    store = UserStore(tmp_path / "users.sqlite")
    uid = 1001

    # Регистрация
    store.register(uid, "testuser", "Test")

    # Списание поиска
    now = time.time()
    assert store.try_use_search(uid, daily_limit=5, now=now) is True
    u = store.get(uid)
    assert u is not None
    assert u.total_searches == 1

    with store._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (uid,)).fetchone()
        assert row["search_count"] == 1
        assert row["total_searches"] == 1

    # Возврат поиска
    assert store.refund_search(uid, now=now) is True

    with store._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (uid,)).fetchone()
        assert row["search_count"] == 0
        assert row["total_searches"] == 0

    # Повторный возврат при 0 не уменьшает ниже 0 и возвращает False
    assert store.refund_search(uid, now=now) is False
    with store._connect() as db:
        row = db.execute("SELECT search_count, total_searches FROM users WHERE tg_id = ?", (uid,)).fetchone()
        assert row["search_count"] == 0
        assert row["total_searches"] == 0


def test_user_store_refund_search_different_day(tmp_path):
    store = UserStore(tmp_path / "users.sqlite")
    uid = 1002

    store.register(uid)
    day1_ts = 1700000000.0  # какой-то день
    assert store.try_use_search(uid, daily_limit=5, now=day1_ts) is True

    # Попытка возврата в другой день не срабатывает
    day2_ts = day1_ts + 86400 * 2
    assert store.refund_search(uid, now=day2_ts) is False

    with store._connect() as db:
        row = db.execute("SELECT search_count FROM users WHERE tg_id = ?", (uid,)).fetchone()
        assert row["search_count"] == 1
