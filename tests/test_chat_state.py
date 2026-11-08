"""КТ4, работа 1: хранилище сессий и выдач (chat_state) — TTL по ходу работы и типы полей, которые оно умеет."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, timedelta
import sqlite3
import types
import typing

import pytest

from avia import chat_state, statedb
from avia.criteria import SearchCriteria
from avia.dialog import Session, Step
from avia.models import Leg, Offer, OfferAlt, Query, SiteResult
from avia.store import ResultStore, SearchSnapshot

TTL = 1800.0


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_SINGLE", "1")
    statedb.forget_ready()
    return statedb.ensure_single_db(tmp_path / "state")


def keys(path, table: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute(f"SELECT chat_key FROM {table}")}


def snapshot(store: ResultStore, chat_id: int) -> SearchSnapshot:
    q = Query("MOW", "AER", date.today() + timedelta(days=10))
    res = [SiteResult("s", True, [Offer("s", 5000, offer_id="a")])]
    return store.put(chat_id, q, res, "any", res[0].offers)


def test_expired_rows_pruned_while_running_and_not_resurrected(db):
    clock = Clock(1_000_000.0)
    store = ResultStore(clock=clock)
    cs = chat_state.ChatStateStore(db, snapshot_ttl=TTL, clock=clock)
    old_snap = snapshot(store, 1)
    cs.save_chat(1, Session(step=Step.DONE), old_snap)
    assert keys(db, "chat_snapshots") == {"1"}

    clock.t += TTL + chat_state.PRUNE_EVERY_SECONDS  # выдача 1 устарела, сессия 1 — ещё нет
    cs.save_chat(2, Session(step=Step.ORIGIN), snapshot(store, 2))
    assert keys(db, "chat_snapshots") == {"2"}
    assert keys(db, "chat_sessions") == {"1", "2"}
    cs.save_chat(1, Session(step=Step.DONE), old_snap)  # устаревшая выдача ещё в памяти ResultStore
    assert keys(db, "chat_snapshots") == {"2"}

    clock.t += chat_state.SESSION_TTL_SECONDS + 1
    cs.save_chat(3, None, None)
    assert keys(db, "chat_sessions") == set()


def test_unchanged_chat_is_not_rewritten(db):
    cs = chat_state.ChatStateStore(db, snapshot_ttl=TTL)
    store = ResultStore()
    snap = snapshot(store, 1)
    session = Session(step=Step.DONE)
    cs.save_chat(1, session, snap)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE chat_sessions SET updated_at = 1")
    cs.save_chat(1, session, snap)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT updated_at FROM chat_sessions").fetchone()[0] == 1
    session.step = Step.MENU
    cs.save_chat(1, session, snap)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT updated_at FROM chat_sessions").fetchone()[0] > 1


def _leaf_types(tp) -> set:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        return set().union(*(_leaf_types(a) for a in typing.get_args(tp)))
    if origin in (list, frozenset):
        return {origin} | set().union(*(_leaf_types(a) for a in typing.get_args(tp)))
    return {tp}


@pytest.mark.parametrize("cls", [Session, SearchSnapshot, Query, SiteResult, Offer, Leg, OfferAlt, SearchCriteria])
def test_every_field_type_is_supported(cls):
    """Поле нового типа (dict с не-строковыми ключами, datetime, tuple, другой Enum) ломало бы сохранение молча."""
    supported = {int, float, str, bool, type(None), date, list, frozenset, Step, Offer, SiteResult, Query, Leg,
                 OfferAlt, SearchCriteria}
    hints = typing.get_type_hints(cls)
    for f in fields(cls):
        leaves = _leaf_types(hints[f.name])
        assert leaves <= supported, f"{cls.__name__}.{f.name}: {leaves - supported}"
        for leaf in leaves:
            assert not is_dataclass(leaf) or leaf.__name__ in chat_state._CLASSES or leaf is SearchSnapshot
