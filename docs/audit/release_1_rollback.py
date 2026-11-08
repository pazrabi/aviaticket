#!/usr/bin/env python3
"""Выпуск 1: откат кода на `main` при базах `state/`, которые создал и заполнил код выпуска.

Три фазы на одном каталоге `state/`, каждая — отдельный процесс со своим кодом бота в PYTHONPATH:
  new  — код этого дерева (release-1) создаёт и заполняет базы;
  old  — код OLD_REF (копия через `git archive`) открывает те же базы, читает и пишет — откат;
  new2 — снова код этого дерева: выкладка выпуска после отката.

В каждой фазе — настоящие `Bot._run_search` (цены есть и «все сайты упали»), `Bot._run_deep_search`,
`/stats`, разделы `/admin` (кроме «Телефоны»: он ходит в хаб), прямые вызовы хранилищ
(metrics: site_runs, attempts, searches, price_timeline, parser_watch_state; users; history;
subscriptions; таблица скоростей), бэкап `backup_all` и `prune`. Поиск подменён заглушкой,
Telegram и HTTP бота — тоже: ни сети, ни телефонов. Провал фазы — исключение в шаге или запись
лога уровня ERROR и выше. Последний шаг — контроль: поиск падает исключением, и перехват лога обязан
увидеть ERROR бота (его записи в провалы не идут).

Запуск интерпретатором с зависимостями бота (requirements.txt), без прокси; каталог --work не должен
существовать:
  <venv>/bin/python -B docs/audit/release_1_rollback.py --work <новый каталог>
--old-src <каталог src> — готовый код вместо `git archive` (контрольная мутация).
Код выхода 0 — все фазы без ошибок, базы целы, ожидания по строкам выполнены.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
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

OLD_REF_DEFAULT = "9ba5ae3"
REPO = Path(__file__).resolve().parents[2]
ADMIN_ID = 1
PERSON_ID = 555  # живой поиск
DEEP_ID = 556  # глубокий поиск
DAILY_LIMIT = 40
SITES = ("site1", "site2")
DEVICES = ("dev1", "dev2")
DBS = ("users.sqlite", "metrics.sqlite", "history.sqlite", "subscriptions.sqlite")
NEW_SEARCH_COLUMNS = (
    "card_seconds", "card_sites_ok", "card_min_price", "full_min_price", "preview_seconds", "preview_min_price",
)
ADMIN_SECTIONS = ("home", "over", "parse", "health", "ord", "users", "db", "tickets", "subs", "backup")
PHASES = ("new", "old", "new2")
CONTROL_STEP = "control_error"  # последний шаг: его ERROR — ожидаемый, в провалы не идёт
DAY_SECONDS = 86400


# --- базы напрямую, мимо кода бота ---


def table_counts(state: Path) -> dict[str, dict[str, int] | None]:
    """Строк в каждой таблице каждой базы."""
    out: dict[str, dict[str, int] | None] = {}
    for name in DBS:
        path = state / name
        if not path.exists():
            out[name] = None
            continue
        with closing(sqlite3.connect(path)) as db:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            out[name] = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    return out


def person_counters(state: Path, uid: int) -> list[Any] | None:
    """(search_day, search_count, total_searches) человека из users.sqlite."""
    path = state / "users.sqlite"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as db:
        row = db.execute(
            "SELECT search_day, search_count, total_searches FROM users WHERE tg_id = ?", (uid,)
        ).fetchone()
    return list(row) if row else None


def searches_new_columns(state: Path) -> dict[str, Any]:
    """Сколько строк searches, у скольких хоть одна новая колонка не NULL; id последних строк."""
    path = state / "metrics.sqlite"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(searches)")]
        present = [c for c in NEW_SEARCH_COLUMNS if c in cols]
        rows = db.execute(f"SELECT id, source, {', '.join(present) or 'NULL'} FROM searches ORDER BY id").fetchall()
    filled = [r[0] for r in rows if any(v is not None for v in r[2:])]
    return {"columns": cols, "rows": len(rows), "ids_with_new_values": filled, "max_id": rows[-1][0] if rows else 0}


def integrity(state: Path) -> dict[str, str]:
    out = {}
    for name in DBS:
        path = state / name
        if path.exists():
            with closing(sqlite3.connect(path)) as db:
                out[name] = str(db.execute("PRAGMA integrity_check").fetchone()[0])
    return out


# --- фаза: код бота из PYTHONPATH ---


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


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
        return [
            SiteResult(name, False, [], error="сайт временно недоступен", device=DEVICES[i], priority=0)
            for i, name in enumerate(SITES)
        ]

    return priced if kind == "priced" else all_down


class _FakeSearch:
    """Подмена avia.bot.search: отдаёт готовые результаты или падает error, без телефонов и сети."""

    def __init__(self, make: Callable[[], list[Any]], error: Exception | None = None) -> None:
        self.make = make
        self.error = error
        self.calls = 0

    async def __call__(self, q: Any, s: Any, *, deep: bool = False, **kwargs: Any) -> list[Any]:
        self.calls += 1
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


def _steps(phase: str, state: Path, work: Path) -> tuple[list[tuple[str, Callable[[], Any]]], dict[str, Any]]:
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

    def make() -> str:
        ctx["bot"] = _make_bot(state)
        for uid in (ADMIN_ID, PERSON_ID, DEEP_ID):
            ctx["bot"].users.register(uid, None, "Имя")
        return f"metrics={'on' if ctx['bot'].metrics is not None else 'off'}"

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
        if phase == "old":
            m.record_search("user", 12.0, res)
        else:
            m.record_search("user", 12.0, res, card_seconds=6.0, card_sites_ok=2, card_min_price=5000.0,
                            full_min_price=5000.0, preview_seconds=1.2, preview_min_price=4900.0)
        m.record_timeline(search_id=f"{phase}-sid", site=SITES[0], device=DEVICES[0], woke=True,
                          t_first_match=1.0, t_last=2.0, final_min=5000, points=[{"t": 1.0, "min": 5000}])
        m.set_watch_state(SITES[0], "ok", now, "ok")
        reads = {
            "summary": len(m.summary(since)),
            "summary_user": len(m.summary(since, source="user")),
            "by_device": len(m.summary_by_device(since)),
            "by_site_device": len(m.summary_by_site_device(since)),
            "recent_errors": len(m.recent_errors(limit=10, since_ts=since)),
            "counts_by_source": m.counts_by_source(since),
            "timelines": len(m.timelines(since_ts=since)),
            "watch_stats": len(m.watch_stats(since)),
            "watch_state": bool(m.get_watch_state(SITES[0])),
            "site_recent_outcomes": len(m.site_recent_outcomes(SITES[0], since)),
        }
        return json.dumps(reads, ensure_ascii=False)

    def speed() -> str:
        reset_speed_tables()
        table = get_speed_table(ctx["bot"].s)
        table.refresh()
        order = table.order_candidates(ctx["bot"].s.sites[0], list(DEVICES), priority=0)
        status = table.get_pair_status(SITES[0], DEVICES[0])
        return f"order={list(order)} status={type(status).__name__}"

    def users() -> str:
        u = ctx["bot"].users
        return json.dumps({
            "count": u.count(), "stats": u.stats(), "recent": len(u.recent()), "top": len(u.top_searchers()),
            "active_7d": u.count_active_since(since), "new_7d": u.count_since_created(since),
        }, ensure_ascii=False)

    def subs() -> str:
        s = ctx["bot"].subs
        sub = s.create(PERSON_ID, query, "any", SearchCriteria(), now=now)
        s.record_price(sub.id, 5000, "RUB", f"{phase}-sub", now)
        s.record_notification(sub.id, f"{phase}-sub", 5000, ["drop"], now)
        s.mark_checked(sub.id, now)
        today = date.fromtimestamp(now)
        return json.dumps({
            "sub_id": sub.id, "active": len(s.list_active(PERSON_ID)), "due": len(s.due(now, today)),
            "prices": len(s.recent_prices(sub.id, 0)), "first": s.first_price(sub.id),
            "reasons": s.notification_reason_counts(0), "overdue": s.overdue_stats(now, today),
            "tables": s.table_stats(), "routes": bool(s.route_stats()),
        }, ensure_ascii=False, default=str)

    def history() -> str:
        h = ctx["bot"].history
        return json.dumps({
            "tables": h.table_stats(), "searches": h.count_searches_since(0), "rows": h.rows_since(0),
            "route_stats": h.route_stats(query) is not None,
        }, ensure_ascii=False, default=str)

    def backup() -> str:
        dest = work / f"backups_{phase}"
        made = backup_all(state, dest, now=now)
        return f"{len(made)} files, listed {len(list_backups(dest))}"

    def prune() -> str:
        return f"metrics={ctx['bot'].metrics.prune(keep_days=90)} history={ctx['bot'].history.prune(keep_days=45)}"

    async def control_error() -> str:
        """Контроль перехвата лога: поиск падает, бот обеих версий пишет log.exception("search failed")."""
        bot = ctx["bot"]
        bot_mod.search = _FakeSearch(_make_results("priced", f"{phase}-control"),
                                     error=RuntimeError("контроль перехвата лога"))
        bot.dialog.sessions[PERSON_ID] = Session(step=Step.SEARCHING, dialog_id=f"{phase}-control")
        await bot._run_search(PERSON_ID, SearchRequest(query=query))
        return f"step={bot.dialog.sessions[PERSON_ID].step.name}"

    return [
        ("bot", make), ("live_priced", live("priced")), ("live_all_down", live("all_down")), ("deep", deep),
        ("stats", stats), ("admin", admin), ("metrics", metrics_rw), ("speed", speed), ("users", users),
        ("subscriptions", subs), ("history", history), ("backup", backup), ("prune", prune),
        (CONTROL_STEP, control_error),
    ], ctx


async def _run_steps(steps: list[tuple[str, Callable[[], Any]]], report: dict[str, Any],
                     capture: _Capture) -> None:
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


def run_phase(phase: str, state: Path, work: Path) -> dict[str, Any]:
    capture = _Capture()
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(capture)
    import avia

    report: dict[str, Any] = {
        "phase": phase, "avia": str(Path(avia.__file__).resolve().parent),
        "before": table_counts(state), "person_before": person_counters(state, PERSON_ID),
        "deep_before": person_counters(state, DEEP_ID), "steps": [], "failed_steps": [],
    }
    steps, ctx = _steps(phase, state, work)
    asyncio.run(_run_steps(steps, report, capture))
    try:
        from avia.metrics import wait_metrics_io  # только в release-1: записи без ожидания
        wait_metrics_io(10.0)
    except ImportError:
        pass
    calls = getattr(ctx.get("bot"), "calls", [])
    lo, hi = report.get("control_records", [0, 0])
    report.update({
        "after": table_counts(state), "person_after": person_counters(state, PERSON_ID),
        "deep_after": person_counters(state, DEEP_ID), "searches": searches_new_columns(state),
        "log": [{"level": r.levelname, "logger": r.name, "msg": r.getMessage()[:300],
                 "exc": bool(r.exc_info)} for r in capture.records],
        "errors": sum(1 for i, r in enumerate(capture.records)
                      if r.levelno >= logging.ERROR and not lo <= i < hi),
        "control_caught": [f"{r.levelname} {r.name}: {r.getMessage()[:80]}" for r in capture.records[lo:hi]
                           if r.levelno >= logging.ERROR],
        "telegram": [[m, p.get("chat_id"), str(p.get("text", ""))[:120]] for m, p in calls],
    })
    return report


# --- оркестратор ---


def _export_old(ref: str, dest: Path) -> Path:
    archive = dest.with_suffix(".tar")
    subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(archive), ref], check=True)
    dest.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    return dest / "src"


def _phase_env(src: Path) -> dict[str, str]:
    """Чистое окружение: без прокси, токенов и флагов из оболочки; .pyc не пишем."""
    keep = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    return {**keep, "PYTHONPATH": str(src), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}


def _delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for db, tables in (after or {}).items():
        prev = (before or {}).get(db) or {}
        changed = {t: n - prev.get(t, 0) for t, n in (tables or {}).items() if n != prev.get(t, 0)}
        if changed:
            out[db] = changed
    return out


def _expectations(reports: dict[str, dict[str, Any]]) -> list[str]:
    """Что должно было случиться по строкам; пустой список — всё так."""
    problems = []
    for phase, rep in reports.items():
        if rep["failed_steps"]:
            problems.append(f"{phase}: упали шаги {rep['failed_steps']}")
        if rep["errors"]:
            problems.append(f"{phase}: в логе {rep['errors']} записей ERROR")
        if not rep["control_caught"]:
            problems.append(f"{phase}: контроль не сработал — перехват лога не видит ERROR бота")
        delta = _delta(rep["before"], rep["after"])
        for db, table in (("metrics.sqlite", "searches"), ("metrics.sqlite", "site_runs"),
                          ("metrics.sqlite", "attempts"), ("history.sqlite", "price_history"),
                          ("subscriptions.sqlite", "sub_prices"), ("subscriptions.sqlite", "notifications")):
            if delta.get(db, {}).get(table, 0) <= 0:
                problems.append(f"{phase}: в {db}:{table} не добавилось строк")
    old = reports.get("old")
    if old:
        prev_max = reports["new"]["searches"]["max_id"]
        old_filled = [i for i in old["searches"]["ids_with_new_values"] if i > prev_max]
        if old_filled:
            problems.append(f"old: строки searches {old_filled} кода 9ba5ae3 с непустыми новыми колонками")
    new = reports.get("new")
    if new and not new["searches"]["ids_with_new_values"]:
        problems.append("new: код release-1 не заполнил новые колонки searches — проверка ничего не проверила")
    return problems


def orchestrate(work: Path, old_ref: str, old_src: Path | None = None) -> int:
    work = work.resolve()
    work.mkdir(parents=True)  # нет каталога — создаём; есть — ошибка: чужое не трогаем
    state = work / "state"
    state.mkdir()
    old = old_src.resolve() if old_src else _export_old(old_ref, work / f"old_{old_ref}")
    sources = {"new": REPO / "src", "old": old, "new2": REPO / "src"}
    reports: dict[str, dict[str, Any]] = {}
    for phase in PHASES:
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--phase", phase, "--work", str(work)],
            cwd=work, env=_phase_env(sources[phase]), capture_output=True, text=True,
        )
        (work / f"phase_{phase}.stderr").write_text(proc.stderr)
        if proc.returncode != 0 or not proc.stdout.strip():
            print(f"фаза {phase}: процесс упал, код {proc.returncode}\n{proc.stderr[-3000:]}")
            return 1
        reports[phase] = json.loads(proc.stdout.strip().splitlines()[-1])
    (work / "report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=1))

    for phase, rep in reports.items():
        print(f"== фаза {phase}: код {rep['avia']}")
        for st in rep["steps"]:
            print(f"  {'ok ' if st['ok'] else 'ERR'} {st['step']}: {st.get('detail') or st.get('error')}")
        print(f"  строки (добавлено): {json.dumps(_delta(rep['before'], rep['after']), ensure_ascii=False)}")
        print(f"  попытки человека {PERSON_ID} (день, за день, всего): {rep['person_before']} → {rep['person_after']}")
        print(f"  попытки человека {DEEP_ID} (глубокий): {rep['deep_before']} → {rep['deep_after']}")
        print(f"  searches: строк {rep['searches']['rows']}, с новыми значениями id {rep['searches']['ids_with_new_values']}")
        levels: dict[str, int] = {}
        for rec in rep["log"]:
            levels[f"{rec['level']} {rec['logger']}"] = levels.get(f"{rec['level']} {rec['logger']}", 0) + 1
        print(f"  лог WARNING+: {json.dumps(levels, ensure_ascii=False)}; ERROR вне контроля: {rep['errors']}")
        print(f"  контроль (поиск падает исключением): {rep['control_caught']}")
        for rec in rep["log"]:
            if rec["level"] in ("ERROR", "CRITICAL"):
                print(f"    {rec['level']} {rec['logger']}: {rec['msg']}")
    checks = integrity(state)
    print(f"== integrity_check: {json.dumps(checks)}")
    print(f"== searches: {', '.join(reports['new2']['searches']['columns'])}")
    problems = _expectations(reports)
    problems += [f"integrity {db}: {res}" for db, res in checks.items() if res != "ok"]
    print("== итог: " + ("OK" if not problems else "ПРОВАЛ\n  " + "\n  ".join(problems)))
    return 0 if not problems else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, required=True, help="новый каталог для state/ и копии старого кода")
    ap.add_argument("--old-ref", default=OLD_REF_DEFAULT, help="код отката (по умолчанию 9ba5ae3)")
    ap.add_argument("--old-src", type=Path, help="готовый каталог src вместо git archive (контрольная мутация)")
    ap.add_argument("--phase", choices=PHASES, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.phase:
        work = args.work.resolve()
        print(json.dumps(run_phase(args.phase, work / "state", work), ensure_ascii=False, default=str))
        return 0
    return orchestrate(args.work, args.old_ref, args.old_src)


if __name__ == "__main__":
    sys.exit(main())
