"""Сессии диалога и последние выдачи в state/avia.sqlite (КТ4, работа 1, ADR-002; только при DB_SINGLE=1).

Что сохраняется. Сессия (dialog.Session) и выдача (store.SearchSnapshot) — простые dataclass: строки,
числа, даты, frozenset (части суток в SearchCriteria), Enum (Step), вложенные Query, SiteResult, Offer,
Leg, OfferAlt. Замыканий, соединений и задач в них нет. Пишутся JSON с метками типов. Два места, которых
нет в полях dataclass:
- атрибуты сверх полей: fresh_cache ставит Offer.cached_age через setattr — пишутся вместе с полями;
- общие объекты: один билет лежит в results, flat и pool сразу — пишется один раз, ссылки по номеру;
  после загрузки это снова один объект, как до рестарта.
Живой поиск в момент рестарта умирает вместе с процессом: сессия в шаге SEARCHING загружается в DONE,
как её снимает _leave_searching, иначе человек получал бы «⏳ Поиск ещё идёт» до конца TTL.

TTL. Выдача — TTL ResultStore владельца (1800 с, store.py), считается от создания выдачи. У сессий в коде
владельца TTL нет: они живут в памяти до рестарта. SESSION_TTL_SECONDS — сутки от последнего изменения:
недописанный вечером диалог продолжается утром; старше суток даты в нём чаще всего уже прошли, и человек
начинает с меню, как сейчас после любого рестарта. Строк по одной на чат: база растёт с числом людей, а не
поисков; старше TTL строки удаляются при старте бота.

Запись — синхронная короткая транзакция на цикле событий, только если сессия или выдача изменились
(сравнение с последним записанным JSON). Замеры — tests/test_db_single_writes.py, docs/audit/kt4_db.md.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import fields, is_dataclass
from datetime import date
from enum import Enum
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable

from . import statedb
from .criteria import SearchCriteria
from .dialog import Session, Step
from .models import Leg, Offer, OfferAlt, Query, SiteResult
from .store import ResultStore, SearchSnapshot

log = logging.getLogger("avia.chat_state")

SESSION_TTL_SECONDS = 86_400.0
FORMAT = 1
_CLASSES = {c.__name__: c for c in (Query, SiteResult, Offer, Leg, OfferAlt, SearchCriteria, Session)}
_ENUMS = {"Step": Step}
_SNAPSHOT_LISTS = ("results", "flat", "pool")


def _enc(v: Any) -> Any:
    if isinstance(v, Enum):  # раньше str: Step — str-Enum, иначе вернётся просто строкой
        return {"__enum__": type(v).__name__, "v": v.value}
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, date):
        return {"__date__": v.isoformat()}
    if isinstance(v, (set, frozenset)):
        return {"__set__": [_enc(x) for x in sorted(v, key=repr)]}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, dict) and all(isinstance(k, str) for k in v):
        return {"__map__": {k: _enc(x) for k, x in v.items()}}
    if is_dataclass(v) and type(v).__name__ in _CLASSES:
        return {"__dc__": type(v).__name__, "f": {k: _enc(x) for k, x in vars(v).items()}}
    raise TypeError(f"не сохраняется в базу: {type(v).__name__}")


def _make(cls: type, values: dict[str, Any]) -> Any:
    """Объект cls из полей; ключи сверх полей __init__ (cached_age) — атрибутами, как их ставил код."""
    init = {f.name for f in fields(cls) if f.init}
    obj = cls(**{k: v for k, v in values.items() if k in init})
    for k, v in values.items():
        if k not in init:
            object.__setattr__(obj, k, v)
    return obj


def _dec(v: Any) -> Any:
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if not isinstance(v, dict):
        return v
    if "__date__" in v:
        return date.fromisoformat(v["__date__"])
    if "__set__" in v:
        return frozenset(_dec(x) for x in v["__set__"])
    if "__enum__" in v:
        return _ENUMS[v["__enum__"]](v["v"])
    if "__map__" in v:
        return {k: _dec(x) for k, x in v["__map__"].items()}
    if "__dc__" in v:
        return _make(_CLASSES[v["__dc__"]], {k: _dec(x) for k, x in v["f"].items()})
    raise ValueError(f"незнакомая запись: {sorted(v)[:3]}")


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def encode_session(session: Session) -> str:
    return _dumps({"format": FORMAT, "session": _enc(session)})


def decode_session(text: str) -> Session:
    return _dec(json.loads(text)["session"])


def encode_snapshot(snap: SearchSnapshot) -> str:
    """Билеты — одним списком offers; results, flat и pool ссылаются на них по номеру."""
    table: list[Any] = []
    index: dict[int, int] = {}

    def ref(o: Offer) -> int:
        if id(o) not in index:
            index[id(o)] = len(table)
            table.append(_enc(o))
        return index[id(o)]

    results = [{"f": {k: _enc(x) for k, x in vars(r).items() if k != "offers"}, "offers": [ref(o) for o in r.offers]}
               for r in snap.results]
    data = {
        "format": FORMAT,
        "fields": {k: _enc(x) for k, x in vars(snap).items() if k not in _SNAPSHOT_LISTS},
        "results": results,
        "flat": [ref(o) for o in snap.flat],
        "pool": [ref(o) for o in snap.pool],
        "offers": table,
    }
    return _dumps(data)


def decode_snapshot(text: str) -> SearchSnapshot:
    data = json.loads(text)
    offers = [_dec(x) for x in data["offers"]]
    results = [_make(SiteResult, {**{k: _dec(x) for k, x in r["f"].items()}, "offers": [offers[i] for i in r["offers"]]})
               for r in data["results"]]
    return _make(SearchSnapshot, {
        **{k: _dec(x) for k, x in data["fields"].items()},
        "results": results,
        "flat": [offers[i] for i in data["flat"]],
        "pool": [offers[i] for i in data["pool"]],
    })


def _chat_key(chat_id: int | str) -> str:
    return json.dumps(chat_id)


def _digest(text: str | None) -> bytes | None:
    """Отпечаток записанного JSON: сравнивать без второй копии выдачи в памяти."""
    return None if text is None else hashlib.blake2b(text.encode(), digest_size=16).digest()


class ChatStateStore:
    """Строки chat_sessions и chat_snapshots: по одной на чат, JSON, время для TTL."""

    def __init__(self, path: Path | str, clock: Callable[[], float] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock
        self._session_saved: dict[str, bytes | None] = {}  # отпечаток последней записи сессии по чату
        self._snapshot_saved: dict[str, bytes | None] = {}

    def _now(self) -> float:
        return (self._clock or time.time)()

    def _rows(self, sql: str) -> list[tuple]:
        with closing(statedb.connect(self.path)) as conn:
            return conn.execute(sql).fetchall()

    def _delete(self, table: str, column: str, before: float) -> None:
        with closing(statedb.connect(self.path)) as conn, conn:
            conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (before,))

    def load_sessions(self, ttl: float = SESSION_TTL_SECONDS, now: float | None = None) -> dict[int | str, Session]:
        """Сессии моложе ttl; SEARCHING → DONE; старше ttl и битые строки — удаляются."""
        now = self._now() if now is None else now
        out: dict[int | str, Session] = {}
        searching = 0
        for key, data, updated_at in self._rows("SELECT chat_key, data, updated_at FROM chat_sessions"):
            if now - updated_at > ttl:
                continue
            try:
                session = decode_session(data)
            except Exception:
                log.warning("сессия чата %s не читается, пропущена", key, exc_info=True)
                continue
            if session.step == Step.SEARCHING:
                session.step = Step.DONE
                searching += 1
            out[json.loads(key)] = session
            self._session_saved[key] = _digest(data)
        self._delete("chat_sessions", "updated_at", now - ttl)
        log.info("сессий из базы: %d (поиск шёл в %d, шаг снят)", len(out), searching)
        return out

    def load_snapshots(self, ttl: float, now: float | None = None) -> dict[int | str, SearchSnapshot]:
        """Выдачи моложе ttl от создания (как ResultStore.get); старше — удаляются."""
        now = self._now() if now is None else now
        out: dict[int | str, SearchSnapshot] = {}
        for key, data, created_at in self._rows("SELECT chat_key, data, created_at FROM chat_snapshots"):
            if now - created_at > ttl:
                continue
            try:
                out[json.loads(key)] = decode_snapshot(data)
            except Exception:
                log.warning("выдача чата %s не читается, пропущена", key, exc_info=True)
                continue
            self._snapshot_saved[key] = _digest(data)
        self._delete("chat_snapshots", "created_at", now - ttl)
        log.info("выдач из базы: %d", len(out))
        return out

    def save_chat(self, chat_id: int | str, session: Session | None, snapshot: SearchSnapshot | None) -> None:
        """Пишет то, что изменилось с прошлой записи; None — строки чата нет (сессию сбросили)."""
        key = _chat_key(chat_id)
        s_json = None if session is None else encode_session(session)
        p_json = None if snapshot is None else encode_snapshot(snapshot)
        s_sig, p_sig = _digest(s_json), _digest(p_json)
        s_new = s_sig != self._session_saved.get(key)
        p_new = p_sig != self._snapshot_saved.get(key)
        if not (s_new or p_new):
            return
        now = self._now()
        with closing(statedb.connect(self.path)) as conn, conn:
            if s_new and s_json is None:
                conn.execute("DELETE FROM chat_sessions WHERE chat_key = ?", (key,))
            elif s_new:
                conn.execute("INSERT INTO chat_sessions (chat_key, data, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(chat_key) DO UPDATE SET data = excluded.data, "
                             "updated_at = excluded.updated_at", (key, s_json, now))
            if p_new and p_json is None:
                conn.execute("DELETE FROM chat_snapshots WHERE chat_key = ?", (key,))
            elif p_new:
                conn.execute("INSERT INTO chat_snapshots (chat_key, data, created_at, updated_at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(chat_key) DO UPDATE SET data = excluded.data, "
                             "created_at = excluded.created_at, updated_at = excluded.updated_at",
                             (key, p_json, snapshot.created_at, now))
        if s_new:
            self._session_saved[key] = s_sig
        if p_new:
            self._snapshot_saved[key] = p_sig


class PersistentResultStore(ResultStore):
    """ResultStore владельца; после put — сохранение чата (on_put). TTL и часы — те же, что у ResultStore."""

    def __init__(self, on_put: Callable[[int | str], None] | None = None) -> None:
        super().__init__()  # аргументы по умолчанию ResultStore: ttl 1800 с и его часы (симулятор их подменяет)
        self.on_put = on_put

    @property
    def ttl(self) -> float:
        return self._ttl

    def now(self) -> float:
        return self._clock()

    def preload(self, snapshots: dict[int | str, SearchSnapshot]) -> None:
        self._data.update(snapshots)

    def peek(self, chat_id: int | str) -> SearchSnapshot | None:
        """Снимок без проверки TTL: что сейчас лежит в памяти (для записи в базу)."""
        return self._data.get(chat_id)

    def put(self, chat_id: int | str, *args: Any, **kwargs: Any) -> SearchSnapshot:
        snap = super().put(chat_id, *args, **kwargs)
        if self.on_put is not None:
            self.on_put(chat_id)
        return snap


def open_chat_state(
    storage_dir: Path | str, on_put: Callable[[int | str], None] | None = None,
) -> tuple[ChatStateStore, PersistentResultStore, dict[int | str, Session]]:
    """Хранилище, ResultStore с выдачами моложе TTL и сессии моложе суток — для нового объекта бота."""
    cs = ChatStateStore(statedb.ensure_single_db(storage_dir))
    store = PersistentResultStore(on_put=on_put)
    store.preload(cs.load_snapshots(ttl=store.ttl, now=store.now()))
    return cs, store, cs.load_sessions()
