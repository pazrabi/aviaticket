"""КТ4, работа 1: рост state/avia.sqlite — байт на поиск, подписку, человека и МБ в месяц на нагрузке прода.

Запуск из корня репозитория (только чтение входных файлов; синтетическая база пишется в пустой каталог OUT):
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -B docs/audit/kt4_db_growth.py SIM.sqlite PROD.json OUT

SIM.sqlite — avia.sqlite прогона A 50/4 из gate --profile quick при DB_SINGLE=1 (каталог $TMPDIR/avia-load-*):
строки, которые записал код бота на живом поиске людей. В A нет подписок и фона: их таблицы и людей заполняет
синтетическая база через API сторов владельца, строки сборов (site_runs, attempts, price_history) — копии строк
симулятора. Байт на строку — dbstat: занятые байты страниц таблицы и её индексов (pgsize − unused) / строк.
PROD.json — выгрузка docs/review/prod_stats.py: попытки по источникам (user, sub, bg) и поиски по дням.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys

PH_CAP = 15  # history_max_offers_per_site владельца: не больше 15 строк price_history на сайт в сборе
DAY = 86_400.0
COPIES = 40  # во сколько раз размножить строки симулятора в синтетической базе


def table_bytes(path: Path) -> dict[str, tuple[int, int]]:
    """{таблица: (строк, занятых байт с индексами)} по dbstat."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        owner = dict(db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table', 'index')"))
        used: dict[str, int] = {}
        for name, n in db.execute("SELECT name, SUM(pgsize - unused) FROM dbstat GROUP BY name"):
            t = owner.get(name, name)
            used[t] = used.get(t, 0) + int(n)
        out = {}
        for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            out[t] = (db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0], used.get(t, 0))
    return out


def copy_rows(db: sqlite3.Connection, table: str, copies: int) -> None:
    cols = [r[1] for r in db.execute(f'PRAGMA sim.table_info("{table}")') if not (r[5] and "INT" in r[2].upper())]
    names = ", ".join(cols)
    for _ in range(copies):
        db.execute(f'INSERT INTO main."{table}" ({names}) SELECT {names} FROM sim."{table}"')


def build_synthetic(sim: Path, out: Path) -> Path:
    os.environ["DB_SINGLE"] = "1"
    from avia import chat_state, statedb
    from avia.criteria import SearchCriteria
    from avia.dialog import Session, Step
    from avia.history import PriceHistory
    from avia.metrics import MetricsStore
    from avia.models import Query
    from avia.subscriptions import SubscriptionStore
    from avia.users import UserStore

    path = statedb.ensure_single_db(out)
    users, subs = UserStore(path), SubscriptionStore(path)
    PriceHistory(path)
    MetricsStore(path)._connect().close()
    cs = chat_state.ChatStateStore(path, snapshot_ttl=1800.0)
    base = date.today() + timedelta(days=20)
    for i in range(1000):
        users.register(10_000_000 + i, username=f"user_{i}", first_name="Имя")
        cs.save_chat(10_000_000 + i, Session(step=Step.DONE, origin="MOW", destination="AER", depart=base,
                                             criteria=SearchCriteria(max_transfers=1)), None)
    for i in range(500):
        q = Query("MOW", ("AER", "LED", "KZN", "OVB", "ALA")[i % 5], base + timedelta(days=i % 60))
        sub = subs.create(10_000_000 + i, q, "any", SearchCriteria(max_transfers=i % 3))
        for k in range(10):
            subs.record_price(sub.id, 5000 + 7 * k, "RUB", f"aviasales-{i}-{k}-SU1234-1005", 1.79e9 + k * 1800)
        subs.record_notification(sub.id, f"aviasales-{i}-SU1234", 4900, ["drop_abs", "drop_pct"], 1.79e9)
    db = sqlite3.connect(f"file:{path}", uri=True)
    db.execute("ATTACH DATABASE ? AS sim", (f"file:{sim}?mode=ro",))
    with db:
        for table in ("site_runs", "attempts", "price_history", "price_timeline", "searches"):
            copy_rows(db, table, COPIES)
        db.executemany("INSERT INTO scheduler_heartbeat (ts) VALUES (?)", [(1.79e9 + 300 * i,) for i in range(2000)])
    db.execute("DETACH DATABASE sim")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    return path


def per_row(stats: dict[str, tuple[int, int]]) -> dict[str, float]:
    return {t: b / n for t, (n, b) in stats.items() if n}


def prod_rates(prod: dict) -> dict[str, float]:
    """В сутки по проду. Попытки покрывают те же дни, что поиски (by_day): на 7 сутках user-попыток вышло бы
    меньше сайтов на поиск (183 primary / 68 поисков = 2,7 < 5,2 сайта) — значит, выгрузка моложе окна."""
    by_day = prod["searches"]["by_day"]
    first = datetime.fromisoformat(min(by_day) + "T00:00:00")
    span = (datetime.fromisoformat(prod["generated_at"]) - first).total_seconds() / DAY
    roles = prod["attempts"]["roles"]
    att = {src: sum(n for k, n in roles.items() if k.startswith(src + "|")) for src in ("user", "sub", "bg")}
    prim = {src: roles.get(f"{src}|primary", 0) for src in ("user", "sub", "bg")}
    fast = sum(d["fast_card"]["n"] for d in by_day.values())
    deep = sum(d["deep_n"] for d in by_day.values())
    return {"span_days": span, "searches": (fast + deep) / span,
            "sites_ok": prod["searches"]["fast"]["sites_ok_mean"], "sites_total": prod["searches"]["fast"]["sites_total_mean"],
            **{f"att_{s}": att[s] / span for s in att}, **{f"prim_{s}": prim[s] / span for s in prim},
            "att_per_search": att["user"] / (fast + deep)}


def main() -> None:
    sim, prod_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    prod = json.loads(prod_path.read_text(encoding="utf-8"))
    s_stats = table_bytes(sim)
    syn = build_synthetic(sim, out)
    y_stats = table_bytes(syn)
    row = {**per_row(y_stats), "chat_snapshots": per_row(s_stats).get("chat_snapshots", 0.0)}
    print("Байт на строку (с индексами):")
    for t in sorted(row):
        print(f"  {t:20} {row[t]:8.0f}   строк: синтетика {y_stats.get(t, (0,))[0]}, симулятор {s_stats.get(t, (0,))[0]}")
    with sqlite3.connect(f"file:{sim}?mode=ro", uri=True) as db:
        n_search = db.execute("SELECT COUNT(*) FROM searches WHERE source = 'user'").fetchone()[0]
        sim_rows = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                    ("site_runs", "price_history", "price_timeline")}
        snaps = db.execute("SELECT AVG(LENGTH(data)), MAX(LENGTH(data)) FROM chat_snapshots").fetchone()
    r = prod_rates(prod)
    print(f"\nПрод ({prod_path.name}): выгрузка за {r['span_days']:.2f} сут; поисков людей {r['searches']:.1f}/сут; "
          f"попыток в сутки user {r['att_user']:.0f}, sub {r['att_sub']:.0f}, bg {r['att_bg']:.0f}; "
          f"сборов (primary) sub {r['prim_sub']:.0f}, bg {r['prim_bg']:.0f}; попыток на поиск {r['att_per_search']:.1f}")
    ph_sim = sim_rows["price_history"] / sim_rows["site_runs"]
    search_rows = {"searches": 1, "site_runs": r["sites_total"], "attempts": r["att_per_search"],
                   "price_timeline": sim_rows["price_timeline"] / n_search, "price_history": PH_CAP * r["sites_ok"]}
    b_search = sum(n * row[t] for t, n in search_rows.items())
    b_search_sim = b_search - (PH_CAP * r["sites_ok"] - ph_sim * r["sites_total"]) * row["price_history"]
    b_collect = row["site_runs"] + row["price_history"] * PH_CAP
    b_att = row["attempts"]
    b_sub_check = row["sub_prices"]  # одна строка цены на проверку подписки (все сайты)
    b_person = row["users"] + row["chat_sessions"] + row["chat_snapshots"]
    print(f"\nЖивой поиск: {b_search:,.0f} Б (price_history по {PH_CAP} на сайт с ценами), "
          f"{b_search_sim:,.0f} Б (как в симуляторе: {ph_sim:.2f} строки истории на сбор сайта)")
    print(f"  строк на поиск: " + ", ".join(f"{t} {n:.1f}" for t, n in search_rows.items()))
    print(f"Сбор сайта подпиской или фоном: {b_collect:,.0f} Б (site_runs + {PH_CAP} строк price_history) "
          f"+ {b_att:.0f} Б на попытку")
    print(f"Подписка: {row['subscriptions']:.0f} Б строка, {b_sub_check:.0f} Б на проверку (sub_prices), "
          f"{row['notifications']:.0f} Б на оповещение")
    print(f"Человек: {b_person:,.0f} Б (users {row['users']:.0f} + сессия {row['chat_sessions']:.0f} + "
          f"выдача {row['chat_snapshots']:.0f}; выдача в симуляторе: средняя {snaps[0]:,.0f} Б, макс. {snaps[1]:,.0f} Б zlib)")
    day = {
        "live": r["searches"] * b_search,
        "sub": r["prim_sub"] * b_collect + r["att_sub"] * b_att + r["prim_sub"] / r["sites_total"] * b_sub_check,
        "bg": r["prim_bg"] * b_collect + r["att_bg"] * b_att,
        "heartbeat": DAY / 300 * row["scheduler_heartbeat"],
    }
    total = sum(day.values())
    print("\nРост в сутки, верхняя граница (каждый сбор пишет 15 строк истории на сайт):")
    for k, v in day.items():
        print(f"  {k:10} {v / 1e6:6.2f} МБ/сут  {v * 30 / 1e6:7.1f} МБ/мес")
    print(f"  {'всего':10} {total / 1e6:6.2f} МБ/сут  {total * 30 / 1e6:7.1f} МБ/мес")
    lo = r["span_days"] / 7.0
    print(f"  при окне 7 сут вместо {r['span_days']:.2f}: {total * lo * 30 / 1e6:.1f} МБ/мес")
    print(f"\nprice_history из этого: {(r['searches'] * PH_CAP * r['sites_ok'] + (r['prim_sub'] + r['prim_bg']) * PH_CAP) * row['price_history'] * 30 / 1e6:.1f} МБ/мес; "
          f"у владельца prune держит её в 45 сутках")


if __name__ == "__main__":
    main()
