#!/usr/bin/env python3
"""Выпуск 2: откат кода на выпуск 1 (`b8909ab`) и откат флага `DB_SINGLE` через `avia.cli db-rollback`.

Основа — `docs/audit/release_1_rollback.py`. Каждая фаза — отдельный процесс со своим кодом бота в PYTHONPATH.

Часть А (каталог <work>/a/state), флаги выпуска выключены:
  new  — код этого дерева (release-2) создаёт и заполняет четыре базы;
  old  — код OLD_REF (копия через `git archive`) открывает те же базы, читает и пишет — откат выпуска;
  new2 — снова release-2: выкладка выпуска после отката.

Часть Б (каталог <work>/b/code/state: `db-rollback` берёт state/ рядом с кодом, поэтому код — копия HEAD):
  legacy — release-2 при DB_SINGLE=0 заполняет четыре файла (как прод до включения флага);
  single — release-2 при DB_SINGLE=1: перенос в avia.sqlite и новые записи с метками «single-…»;
  `python -m avia.cli db-rollback` (DB_SINGLE=1 в окружении, как в .env на сервере в момент отката);
  old_after — код OLD_REF (флаг не читает) открывает четыре файла: видит каждую запись с меткой «single-…»,
             читает и пишет без ошибок.

В каждой фазе — настоящие `Bot._run_search` (цены есть и «все сайты упали»), `Bot._run_deep_search`, `/stats`,
разделы `/admin` (кроме «Телефоны»: он ходит в хаб), прямые вызовы хранилищ (site_runs, attempts, searches,
price_timeline, parser_watch_state, users, history, subscriptions, таблица скоростей), бэкап `backup_all`, `prune`.
Поиск, Telegram и HTTP бота подменены: ни сети, ни телефонов. Провал фазы — исключение в шаге или запись лога
уровня ERROR и выше. Последний шаг — контроль: поиск падает исключением, и перехват лога обязан увидеть ERROR бота
(его записи в провалы не идут).

Запуск интерпретатором с зависимостями бота (requirements.txt), без прокси; каталог --work не должен существовать:
  <venv>/bin/python -B docs/audit/release_2_rollback.py --work <новый каталог>
Контрольные мутации: --old-src <каталог src> — готовый код вместо `git archive OLD_REF`;
--drop-after-rollback <таблица> — после db-rollback удалить записи фазы single (проверка меток должна упасть).
Код выхода 0 — обе части без ошибок, базы целы, ожидания выполнены.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time
import traceback
from typing import Any, Callable

OLD_REF_DEFAULT = "b8909ab"
REPO = Path(__file__).resolve().parents[2]
ADMIN_ID = 1
PERSON_ID = 555  # живой поиск
DEEP_ID = 556  # глубокий поиск
MARK_USER_BASE = 900  # 900 + номер фазы: человек, которого регистрирует только эта фаза
DAILY_LIMIT = 40
SITES = ("site1", "site2")
DEVICES = ("dev1", "dev2")
GROUPS = ("users", "metrics", "history", "subscriptions")
LEGACY_FILES = tuple(f"{g}.sqlite" for g in GROUPS)
SINGLE_DB = "avia.sqlite"
KT4_TABLES = frozenset({"db_meta", "db_legacy_tables", "chat_sessions", "chat_snapshots"})  # statedb.KT4_TABLES
SCHEMA_VERSION = 2  # statedb.MIGRATIONS[-1][0] на release-2
ADMIN_SECTIONS = ("home", "over", "parse", "health", "ord", "users", "db", "tickets", "subs", "backup")
PHASES = ("new", "old", "new2", "legacy", "single", "old_after")
ROWS_EXPECTED = ("searches", "site_runs", "attempts", "price_history", "sub_prices", "notifications")
CONTROL_STEP = "control_error"
FLAG_VARS = ("DB_SINGLE", "SUB_PREFILTER", "SUB_PREFILTER_PCT", "SUB_PREFILTER_FULL_HOURS")
DAY_SECONDS = 86400


# --- базы напрямую, мимо кода бота ---


def db_file(state: Path, group: str) -> Path:
    """Файл, где лежит группа таблиц в этой фазе: при DB_SINGLE=1 — avia.sqlite."""
    return state / (SINGLE_DB if os.environ.get("DB_SINGLE", "0").strip() == "1" else f"{group}.sqlite")


def table_counts(state: Path) -> dict[str, dict[str, int]]:
    """{файл: {таблица: строк}} для четырёх прежних файлов и avia.sqlite, какие есть."""
    out: dict[str, dict[str, int]] = {}
    for name in (*LEGACY_FILES, SINGLE_DB):
        path = state / name
        if not path.is_file():
            continue
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out[name] = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    return out


def by_table(counts: dict[str, dict[str, int]], files: tuple[str, ...]) -> dict[str, int]:
    """Строк по имени таблицы в файлах files (имена таблиц в четырёх базах не совпадают)."""
    out: dict[str, int] = {}
    for name in files:
        for table, n in counts.get(name, {}).items():
            out[table] = out.get(table, 0) + n
    return out


def person_counters(state: Path, uid: int) -> list[Any] | None:
    path = db_file(state, "users")
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT search_day, search_count, total_searches FROM users WHERE tg_id = ?",
                         (uid,)).fetchone()
    return list(row) if row else None


def integrity(state: Path) -> dict[str, str]:
    out = {}
    for name in (*LEGACY_FILES, SINGLE_DB):
        path = state / name
        if path.is_file():
            with closing(sqlite3.connect(path)) as db:
                out[name] = str(db.execute("PRAGMA integrity_check").fetchone()[0])
    return out


def file_digests(state: Path) -> dict[str, str]:
    return {name: hashlib.sha256((state / name).read_bytes()).hexdigest()[:16]
            for name in LEGACY_FILES if (state / name).is_file()}


# --- фаза: код бота из PYTHONPATH ---


class _Capture(logging.Handler):
    def __init__(self, level: int) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _StateLog(logging.Filter):
    """INFO переноса и состояния чатов — в отчёт фазы (не провал)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name in ("avia.statedb", "avia.chat_state")


def _make_results(kind: str, tag: str) -> Callable[[], list[Any]]:
    from avia.models import Offer, SiteResult

    def priced() -> list[Any]:
        return [
            SiteResult(
                name, True,
                [Offer(name, 5000 + 100 * i, airline="TestAir", offer_id=f"{tag}-{name}",
                       depart_time="07:05", arrive_time="10:15", duration="3ч 10м", transfers=0)],
                elapsed=3.0 + i, device=DEVICES[i], fetch_seconds=2.5, priority=0,
            )
            for i, name in enumerate(SITES)
        ]

    def all_down() -> list[Any]:
        return [SiteResult(name, False, [], error="сайт временно недоступен", device=DEVICES[i], priority=0)
                for i, name in enumerate(SITES)]

    return priced if kind == "priced" else all_down


class _FakeSearch:
    """Подмена avia.bot.search: отдаёт готовые результаты или падает error, без телефонов и сети."""

    def __init__(self, make: Callable[[], list[Any]], error: Exception | None = None) -> None:
        self.make = make
        self.error = error

    async def __call__(self, q: Any, s: Any, *, deep: bool = False, **kwargs: Any) -> list[Any]:
        if self.error is not None:
            raise self.error
        return self.make()


def _make_bot(state: Path) -> Any:
    import httpx

    from avia.bot import Bot
    from avia.config import Settings, SiteConfig

    def refuse(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"сеть запрещена в проверке отката: {request.url}")

    class LocalBot(Bot):
        def __init__(self, settings: Any) -> None:
            super().__init__(settings)
            self.http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self._next_msg_id = 200

        async def call(self, method: str, **params: Any) -> Any:
            self.calls.append((method, params))
            if method == "sendMessage":
                self._next_msg_id += 1
                return {"message_id": self._next_msg_id, "chat": {"id": params.get("chat_id")}}
            if method in ("answerCallbackQuery", "deleteMessage", "editMessageText"):
                return True
            if method == "getUpdates":
                return []
            return {"ok": True}

        async def send_photo(self, chat_id: int, png_bytes: bytes, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(("sendPhoto", {"chat_id": chat_id, "bytes": len(png_bytes)}))
            return {"message_id": 1}

    settings = Settings(
        sites=[SiteConfig(name=name, url_template=f"http://{name}.invalid/{{origin}}") for name in SITES],
        tg_token="t",
        tg_allowed_ids={ADMIN_ID},
        tg_open=True,
        daily_search_limit=DAILY_LIMIT,
        storage_dir=state,
    )
    return LocalBot(settings)


def _markers(state: Path, expect: str, ctx: dict[str, Any]) -> str:
    """Записи фазы expect (метка «<фаза>-…» и её человек) видны этому коду: стор бота и SQL по файлам."""
    bot = ctx["bot"]
    uid = MARK_USER_BASE + PHASES.index(expect)
    found = {"user (UserStore.get)": int(bot.users.get(uid) is not None)}
    queries = (
        ("metrics", "attempts", "search_id = ?", f"{expect}-sid"),
        ("metrics", "price_timeline", "search_id = ?", f"{expect}-sid"),
        ("history", "price_history", "offer_id LIKE ?", f"{expect}-%"),
        ("subscriptions", "sub_prices", "offer_id = ?", f"{expect}-sub"),
        ("subscriptions", "notifications", "offer_id = ?", f"{expect}-sub"),
        ("metrics", "searches", "card_seconds = ?", 6.25 + PHASES.index(expect)),
    )
    for group, table, where, arg in queries:
        with closing(sqlite3.connect(db_file(state, group))) as db:
            found[table] = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", (arg,)).fetchone()[0]
    missing = [k for k, n in found.items() if n < 1]
    if missing:
        raise AssertionError(f"записей фазы {expect} не видно: {missing}; найдено {found}")
    return json.dumps(found, ensure_ascii=False)


def _steps(phase: str, state: Path, work: Path, expect: str | None) -> tuple[list[tuple[str, Callable[[], Any]]], dict]:
    """Шаги фазы по порядку — (имя, функция или корутина) — и ctx, где после первого шага лежит бот."""
    from datetime import date

    import avia.bot as bot_mod
    from avia.backup import backup_all, list_backups
    from avia.criteria import SearchCriteria
    from avia.dialog import DeepSearchRequest, SearchRequest, Session, Step
    from avia.models import Query
    from avia.speed import get_speed_table, reset_speed_tables

    query = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 10))
    ctx: dict[str, Any] = {}
    now = time.time()
    since = now - 7 * DAY_SECONDS
    mark_user = MARK_USER_BASE + PHASES.index(phase)

    def make() -> str:
        ctx["bot"] = _make_bot(state)
        for uid in (ADMIN_ID, PERSON_ID, DEEP_ID, mark_user):
            ctx["bot"].users.register(uid, None, "Имя")
        return f"metrics={'on' if ctx['bot'].metrics is not None else 'off'} users={ctx['bot'].users.path.name}"

    def live(kind: str) -> Callable[[], Any]:
        async def run() -> str:
            bot = ctx["bot"]
            bot_mod.search = _FakeSearch(_make_results(kind, f"{phase}-live-{kind}"))
            bot.dialog.sessions[PERSON_ID] = Session(step=Step.SEARCHING, dialog_id=f"{phase}-{kind}")
            await bot._run_search(PERSON_ID, SearchRequest(query=query))
            return f"step={bot.dialog.sessions[PERSON_ID].step.name}"
        return run

    async def deep() -> str:
        bot = ctx["bot"]
        make_results = _make_results("priced", f"{phase}-deep")
        bot_mod.search = _FakeSearch(make_results)
        res = make_results()
        bot.store.put(DEEP_ID, query, res, "any", [o for r in res for o in r.offers])
        bot.dialog.sessions[DEEP_ID] = Session(step=Step.DONE, dialog_id=f"{phase}-deep")
        await bot._run_deep_search(DEEP_ID, DeepSearchRequest(query=query))
        return "ok"

    async def stats() -> str:
        await ctx["bot"]._admin_command(ADMIN_ID, "/stats")
        return "ok"

    async def admin() -> str:
        done = []
        for section in ADMIN_SECTIONS:
            text, _markup = await ctx["bot"]._render_admin_panel(section)
            done.append(f"{section}:{len(text)}")
        return " ".join(done)

    def metrics_rw() -> str:
        m = ctx["bot"].metrics
        res = _make_results("priced", f"{phase}-direct")()
        m.record_results("bg", res, priority=1)
        m.record_attempt(site=SITES[0], device=DEVICES[0], source="user", role="primary", outcome="ok",
                         offers=1, fetch_seconds=2.5, queue_wait=0.1, search_id=f"{phase}-sid",
                         stop_reason="quiet", t_first_offer=1.0, t_last_improve=2.0)
        m.record_attempt(site=SITES[1], device=DEVICES[1], source="user", role="retry", outcome="captcha")
        # card_seconds = 6.25 + номер фазы — метка строки searches этой фазы (b8909ab и release-2 её пишут)
        m.record_search("user", 12.0, res, card_seconds=6.25 + PHASES.index(phase), card_sites_ok=2,
                        card_min_price=5000.0, full_min_price=5000.0, preview_seconds=1.2, preview_min_price=4900.0)
        m.record_timeline(search_id=f"{phase}-sid", site=SITES[0], device=DEVICES[0], woke=True,
                          t_first_match=1.0, t_last=2.0, final_min=5000, points=[{"t": 1.0, "min": 5000}])
        m.set_watch_state(SITES[0], "ok", now, "ok")
        return json.dumps({
            "summary": len(m.summary(since)), "by_device": len(m.summary_by_device(since)),
            "by_site_device": len(m.summary_by_site_device(since)),
            "recent_errors": len(m.recent_errors(limit=10, since_ts=since)),
            "counts_by_source": m.counts_by_source(since), "timelines": len(m.timelines(since_ts=since)),
            "watch_stats": len(m.watch_stats(since)), "watch_state": bool(m.get_watch_state(SITES[0])),
            "site_recent_outcomes": len(m.site_recent_outcomes(SITES[0], since)),
        }, ensure_ascii=False)

    def speed() -> str:
        reset_speed_tables()
        table = get_speed_table(ctx["bot"].s)
        table.refresh()
        order = table.order_candidates(ctx["bot"].s.sites[0], list(DEVICES), priority=0)
        status = table.get_pair_status(SITES[0], DEVICES[0])
        return f"order={list(order)} status={type(status).__name__} db={Path(table.metrics_path).name}"

    def users() -> str:
        u = ctx["bot"].users
        return json.dumps({"count": u.count(), "stats": u.stats(), "recent": len(u.recent()),
                           "top": len(u.top_searchers()), "active_7d": u.count_active_since(since),
                           "new_7d": u.count_since_created(since)}, ensure_ascii=False)

    def subs() -> str:
        s = ctx["bot"].subs
        sub = s.create(PERSON_ID, query, "any", SearchCriteria(), now=now)
        s.record_price(sub.id, 5000, "RUB", f"{phase}-sub", now)
        s.record_notification(sub.id, f"{phase}-sub", 5000, ["drop"], now)
        s.mark_checked(sub.id, now)
        today = date.fromtimestamp(now)
        return json.dumps({"sub_id": sub.id, "active": len(s.list_active(PERSON_ID)), "due": len(s.due(now, today)),
                           "prices": len(s.recent_prices(sub.id, 0)), "first": s.first_price(sub.id),
                           "reasons": s.notification_reason_counts(0), "tables": s.table_stats()},
                          ensure_ascii=False, default=str)

    def history() -> str:
        h = ctx["bot"].history
        return json.dumps({"tables": h.table_stats(), "searches": h.count_searches_since(0),
                           "rows": h.rows_since(0), "route_stats": h.route_stats(query) is not None},
                          ensure_ascii=False, default=str)

    def backup() -> str:
        dest = work / f"backups_{phase}"
        made = backup_all(state, dest, now=now)
        return f"{len(made)} files: {sorted(p.name.split('-')[0] for p in made)}, listed {len(list_backups(dest))}"

    def prune() -> str:
        return f"metrics={ctx['bot'].metrics.prune(keep_days=90)} history={ctx['bot'].history.prune(keep_days=45)}"

    async def control_error() -> str:
        """Контроль перехвата лога: поиск падает, бот пишет log.exception("search failed")."""
        bot = ctx["bot"]
        bot_mod.search = _FakeSearch(_make_results("priced", f"{phase}-control"),
                                     error=RuntimeError("контроль перехвата лога"))
        bot.dialog.sessions[PERSON_ID] = Session(step=Step.SEARCHING, dialog_id=f"{phase}-control")
        await bot._run_search(PERSON_ID, SearchRequest(query=query))
        return f"step={bot.dialog.sessions[PERSON_ID].step.name}"

    steps: list[tuple[str, Callable[[], Any]]] = [("bot", make)]
    if expect:  # сначала — видны ли чужие записи, до своих
        steps.append((f"markers_{expect}", lambda: _markers(state, expect, ctx)))
    steps += [
        ("live_priced", live("priced")), ("live_all_down", live("all_down")), ("deep", deep),
        ("stats", stats), ("admin", admin), ("metrics", metrics_rw), ("speed", speed), ("users", users),
        ("subscriptions", subs), ("history", history), ("backup", backup), ("prune", prune),
        (CONTROL_STEP, control_error),
    ]
    return steps, ctx


async def _run_steps(steps: list[tuple[str, Callable[[], Any]]], report: dict[str, Any], capture: _Capture) -> None:
    """Все шаги в одном цикле событий: объекты бота не переходят между циклами."""
    for name, fn in steps:
        t0 = time.perf_counter()
        first_record = len(capture.records)
        try:
            detail = fn()
            if inspect.isawaitable(detail):
                detail = await detail
            report["steps"].append({"step": name, "ok": True, "detail": detail,
                                    "seconds": round(time.perf_counter() - t0, 2)})
        except Exception as exc:  # шаг упал — фаза провалена, остальные шаги всё равно идут
            report["steps"].append({"step": name, "ok": False, "error": repr(exc),
                                    "traceback": traceback.format_exc(limit=6)})
            report["failed_steps"].append(name)
            if name == "bot":
                return
        finally:
            if name == CONTROL_STEP:
                report["control_records"] = [first_record, len(capture.records)]


def run_phase(phase: str, state: Path, work: Path, expect: str | None) -> dict[str, Any]:
    capture = _Capture(logging.WARNING)
    state_log = _Capture(logging.INFO)
    state_log.addFilter(_StateLog())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(capture)
    root.addHandler(state_log)
    import avia

    report: dict[str, Any] = {
        "phase": phase, "avia": str(Path(avia.__file__).resolve().parent),
        "db_single": os.environ.get("DB_SINGLE", ""), "before": table_counts(state),
        "person_before": person_counters(state, PERSON_ID), "steps": [], "failed_steps": [],
    }
    steps, ctx = _steps(phase, state, work, expect)
    asyncio.run(_run_steps(steps, report, capture))
    try:
        from avia.metrics import wait_metrics_io
        wait_metrics_io(10.0)
    except ImportError:
        pass
    lo, hi = report.get("control_records", [0, 0])
    report.update({
        "after": table_counts(state), "person_after": person_counters(state, PERSON_ID),
        "log": [{"level": r.levelname, "logger": r.name, "msg": r.getMessage()[:300]} for r in capture.records],
        "state_log": [f"{r.name}: {r.getMessage()[:300]}" for r in state_log.records],
        "errors": sum(1 for i, r in enumerate(capture.records) if r.levelno >= logging.ERROR and not lo <= i < hi),
        "control_caught": [f"{r.levelname} {r.name}: {r.getMessage()[:80]}" for r in capture.records[lo:hi]
                           if r.levelno >= logging.ERROR],
    })
    return report


# --- оркестратор ---


def _git_archive(ref: str, dest: Path, paths: tuple[str, ...] = ()) -> Path:
    archive = dest.with_suffix(".tar")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(archive), ref, *paths], check=True)
    dest.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    return dest


def _env(src: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Чистое окружение: без прокси, токенов и флагов из оболочки; .pyc не пишем."""
    keep = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    return {**keep, "PYTHONPATH": str(src), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            **(extra or {})}


def _phase(phase: str, src: Path, state: Path, work: Path, cwd: Path,
           extra: dict[str, str] | None = None, expect: str | None = None) -> dict[str, Any]:
    cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "--phase", phase,
           "--state", str(state), "--work", str(work)]
    if expect:
        cmd += ["--expect", expect]
    proc = subprocess.run(cmd, cwd=cwd, env=_env(src, extra), capture_output=True, text=True)
    (work / f"phase_{phase}.stderr").write_text(proc.stderr)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"фаза {phase}: процесс упал, код {proc.returncode}\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _delta(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, tables in after.items():
        prev = before.get(name, {})
        changed = {t: n - prev.get(t, 0) for t, n in tables.items() if n != prev.get(t, 0)}
        if changed:
            out[name] = changed
    return out


def _phase_problems(rep: dict[str, Any]) -> list[str]:
    phase = rep["phase"]
    problems = []
    if rep["failed_steps"]:
        problems.append(f"{phase}: упали шаги {rep['failed_steps']}")
    if rep["errors"]:
        problems.append(f"{phase}: в логе {rep['errors']} записей ERROR")
    if not rep["control_caught"]:
        problems.append(f"{phase}: контроль не сработал — перехват лога не видит ERROR бота")
    files = (SINGLE_DB,) if rep["db_single"] == "1" else LEGACY_FILES
    before, after = by_table(rep["before"], files), by_table(rep["after"], files)
    for table in ROWS_EXPECTED:
        if after.get(table, 0) <= before.get(table, 0):
            problems.append(f"{phase}: в {table} не добавилось строк")
    return problems


def _print_phase(rep: dict[str, Any]) -> None:
    print(f"== фаза {rep['phase']}: код {rep['avia']}" + (" (DB_SINGLE=1)" if rep["db_single"] == "1" else ""))
    for st in rep["steps"]:
        print(f"  {'ok ' if st['ok'] else 'ERR'} {st['step']}: {st.get('detail') or st.get('error')}")
    print(f"  строки (добавлено): {json.dumps(_delta(rep['before'], rep['after']), ensure_ascii=False)}")
    print(f"  попытки человека {PERSON_ID} (день, за день, всего): {rep['person_before']} → {rep['person_after']}")
    levels: dict[str, int] = {}
    for rec in rep["log"]:
        key = f"{rec['level']} {rec['logger']}"
        levels[key] = levels.get(key, 0) + 1
    print(f"  лог WARNING+: {json.dumps(levels, ensure_ascii=False)}; ERROR вне контроля: {rep['errors']}")
    print(f"  контроль (поиск падает исключением): {rep['control_caught']}")
    for rec in rep["log"]:
        if rec["level"] in ("ERROR", "CRITICAL"):
            print(f"    {rec['level']} {rec['logger']}: {rec['msg']}")
    for line in rep["state_log"]:
        print(f"  лог: {line}")


def part_a(work: Path, old_src: Path) -> list[str]:
    print("######## часть А: release-2 → OLD_REF → release-2, флаги выключены")
    base = work / "a"
    state = base / "state"
    state.mkdir(parents=True)
    problems: list[str] = []
    for phase, src in (("new", REPO / "src"), ("old", old_src), ("new2", REPO / "src")):
        rep = _phase(phase, src, state, base, cwd=base)
        _print_phase(rep)
        problems += _phase_problems(rep)
    if (state / SINGLE_DB).exists():
        problems.append("часть А: появилась avia.sqlite при выключенном DB_SINGLE")
    checks = integrity(state)
    print(f"== integrity_check: {json.dumps(checks)}")
    problems += [f"А: integrity {db}: {res}" for db, res in checks.items() if res != "ok"]
    return problems


def _owner_counts(counts: dict[str, dict[str, int]], files: tuple[str, ...]) -> dict[str, int]:
    return {t: n for t, n in by_table(counts, files).items() if t not in KT4_TABLES}


def _check_single(state: Path, legacy_digests: dict[str, str]) -> list[str]:
    problems = []
    path = state / SINGLE_DB
    if not path.is_file():
        return ["single: нет avia.sqlite после старта при DB_SINGLE=1"]
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        kt4 = sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                     if r[0] in KT4_TABLES)
        chats = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("chat_sessions", "chat_snapshots")}
    print(f"  avia.sqlite: user_version {version}, journal_mode {mode}, таблицы КТ4 {kt4}, чаты {chats}")
    if version != SCHEMA_VERSION:
        problems.append(f"single: user_version {version}, ждали {SCHEMA_VERSION}")
    if set(kt4) != KT4_TABLES:
        problems.append(f"single: таблицы КТ4 {kt4}")
    after = file_digests(state)
    changed = [n for n, d in legacy_digests.items() if after.get(n) != d]
    print(f"  четыре файла после фазы single: {'не изменились' if not changed else 'ИЗМЕНИЛИСЬ ' + str(changed)}")
    if changed:
        problems.append(f"single: старые файлы изменились {changed}")
    return problems


def _db_rollback(code: Path, state: Path, work: Path) -> tuple[list[str], dict[str, int]]:
    """`python -m avia.cli db-rollback` из копии кода: state/ рядом с ней — каталог части Б."""
    single_before = _owner_counts(table_counts(state), (SINGLE_DB,))
    proc = subprocess.run([sys.executable, "-B", "-m", "avia.cli", "db-rollback"], cwd=code,
                          env=_env(code / "src", {"DB_SINGLE": "1"}), capture_output=True, text=True)
    (work / "db_rollback.stderr").write_text(proc.stderr)
    print(f"== python -m avia.cli db-rollback (DB_SINGLE=1 в окружении): код {proc.returncode}")
    for line in proc.stdout.strip().splitlines():
        print(f"  {line}")
    problems = [] if proc.returncode == 0 else [f"db-rollback: код {proc.returncode}: {proc.stderr[-1500:]}"]
    names = sorted(p.name for p in state.iterdir() if p.is_file())
    aside = [n for n in names if ".rolledback-" in n or ".pre-rollback-" in n]
    print(f"  state/ после: {', '.join(n for n in names if n not in aside)}; отложены: {', '.join(aside)}")
    if (state / SINGLE_DB).exists():
        problems.append("db-rollback: avia.sqlite осталась на месте")
    if not any(n.startswith(f"{SINGLE_DB}.rolledback-") for n in aside):
        problems.append("db-rollback: нет avia.sqlite.rolledback-*")
    sidecar = ("-wal", "-shm", "-journal")
    set_aside = {name for name in LEGACY_FILES for n in aside
                 if n.startswith(f"{name}.pre-rollback-") and not n.endswith(sidecar)}
    if set_aside != set(LEGACY_FILES):
        problems.append(f"db-rollback: в *.pre-rollback-* отложены не все прежние файлы: {sorted(set_aside)}")
    legacy_after = _owner_counts(table_counts(state), LEGACY_FILES)
    diff = {t: (single_before.get(t), legacy_after.get(t)) for t in set(single_before) | set(legacy_after)
            if single_before.get(t) != legacy_after.get(t)}
    print(f"  строк по таблицам: avia.sqlite до отката = четыре файла после: {'да' if not diff else 'НЕТ ' + str(diff)}"
          f" ({sum(single_before.values())} строк в {len(single_before)} таблицах)")
    if diff:
        problems.append(f"db-rollback: строки разошлись {diff}")
    return problems, single_before


MUTATION_WHERE = {  # контрольная мутация --drop-after-rollback: записи фазы single, которые удалить после отката
    "sub_prices": ("subscriptions.sqlite", "offer_id = 'single-sub'"),
    "attempts": ("metrics.sqlite", "search_id = 'single-sid'"),
    "users": ("users.sqlite", f"tg_id = {MARK_USER_BASE + PHASES.index('single')}"),
}


def _drop_after_rollback(state: Path, table: str) -> None:
    name, where = MUTATION_WHERE[table]
    with closing(sqlite3.connect(state / name)) as db:
        n = db.execute(f"DELETE FROM {table} WHERE {where}").rowcount
        db.commit()
    print(f"  МУТАЦИЯ: из {name}:{table} удалено {n} строк фазы single")


def part_b(work: Path, old_src: Path, drop: str | None = None) -> list[str]:
    print("######## часть Б: DB_SINGLE=1 → db-rollback → OLD_REF")
    base = work / "b"
    dirty = subprocess.run(["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--", "src", "config"]).returncode
    if dirty:
        return ["Б: src или config рабочего дерева отличаются от HEAD — копия HEAD была бы не тем кодом"]
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    code = _git_archive("HEAD", base / "code", ("src", "config"))
    print(f"  код release-2: копия HEAD {head} (src и config = рабочее дерево) в {code}")
    state = code / "state"
    state.mkdir()
    problems: list[str] = []
    rep = _phase("legacy", code / "src", state, base, cwd=code)
    _print_phase(rep)
    problems += _phase_problems(rep)
    digests = file_digests(state)
    rep = _phase("single", code / "src", state, base, cwd=code, extra={"DB_SINGLE": "1"})
    _print_phase(rep)
    problems += _phase_problems(rep) + _check_single(state, digests)
    rollback_problems, _single = _db_rollback(code, state, base)
    problems += rollback_problems
    if drop:
        _drop_after_rollback(state, drop)
    rep = _phase("old_after", old_src, state, base, cwd=code, expect="single")
    _print_phase(rep)
    problems += _phase_problems(rep)
    checks = integrity(state)
    print(f"== integrity_check: {json.dumps(checks)}")
    problems += [f"Б: integrity {db}: {res}" for db, res in checks.items() if res != "ok"]
    return problems


def orchestrate(work: Path, old_ref: str, old_src: Path | None, drop: str | None = None) -> int:
    work = work.resolve()
    work.mkdir(parents=True)  # нет каталога — создаём; есть — ошибка: чужое не трогаем
    if old_src:
        old = old_src.resolve()
    else:
        old = _git_archive(old_ref, work / f"old_{old_ref}") / "src"
    print(f"OLD_REF {old_ref}: {old}")
    problems: list[str] = []
    for part in (lambda: part_a(work, old), lambda: part_b(work, old, drop)):
        try:
            problems += part()
        except RuntimeError as exc:
            problems.append(str(exc))
    print("== итог: " + ("OK" if not problems else "ПРОВАЛ\n  " + "\n  ".join(problems)))
    return 0 if not problems else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, required=True, help="новый каталог для баз и копий кода")
    ap.add_argument("--old-ref", default=OLD_REF_DEFAULT, help="код отката (по умолчанию b8909ab, выпуск 1)")
    ap.add_argument("--old-src", type=Path, help="готовый каталог src вместо git archive (контрольная мутация)")
    ap.add_argument("--drop-after-rollback", choices=sorted(MUTATION_WHERE),
                    help="контрольная мутация: после db-rollback удалить записи фазы single из таблицы")
    ap.add_argument("--phase", choices=PHASES, help=argparse.SUPPRESS)
    ap.add_argument("--state", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--expect", choices=PHASES, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.phase:
        report = run_phase(args.phase, args.state.resolve(), args.work.resolve(), args.expect)
        print(json.dumps(report, ensure_ascii=False, default=str))
        return 0
    return orchestrate(args.work, args.old_ref, args.old_src, args.drop_after_rollback)


if __name__ == "__main__":
    sys.exit(main())
