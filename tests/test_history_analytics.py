"""Тесты аналитики, сводок и миграции истории цен."""
from datetime import date, datetime, timedelta
import sqlite3
import statistics
import time

import pytest

from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult

Q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))
Q_ROUND = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1), return_date=date(2026, 12, 10))


def _make_res(offers: list[Offer], site: str = "s") -> list[SiteResult]:
    return [SiteResult(site=site, ok=True, offers=offers)]


def test_schema_migration_preserves_data_and_is_idempotent(tmp_path):
    """Миграция старой схемы SQLite добавляет новые колонки и индексы без потери данных."""
    db_path = tmp_path / "old_history.sqlite"
    # Создаём базу со старой схемой (11 колонок)
    old_schema = """
    CREATE TABLE price_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id    TEXT NOT NULL,
        site        TEXT NOT NULL,
        origin      TEXT NOT NULL,
        destination TEXT NOT NULL,
        depart      TEXT NOT NULL,
        return_date TEXT,
        adults      INTEGER NOT NULL,
        price       INTEGER NOT NULL,
        currency    TEXT NOT NULL,
        observed_at REAL NOT NULL
    );
    CREATE INDEX ix_price_offer ON price_history (offer_id, observed_at);
    CREATE INDEX ix_price_route ON price_history (origin, destination, depart, return_date);
    """
    with sqlite3.connect(db_path) as db:
        db.executescript(old_schema)
        db.execute(
            "INSERT INTO price_history "
            "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old_offer_1", "s1", "KUF", "EVN", "2026-12-01", None, 1, 10500, "RUB", 1000.0),
        )

    # Открываем через PriceHistory — должна произойти миграция
    h = PriceHistory(db_path)
    with h._connect() as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(price_history)").fetchall()}
        expected_new_cols = {
            "days_to_departure",
            "baggage",
            "airline",
            "flight_numbers",
            "depart_at",
            "search_id",
        }
        assert expected_new_cols.issubset(cols)

        # Проверяем сохранность старых данных
        row = db.execute(
            "SELECT offer_id, price, days_to_departure, baggage, airline FROM price_history WHERE offer_id = ?",
            ("old_offer_1",),
        ).fetchone()
        assert row[0] == "old_offer_1"
        assert row[1] == 10500
        # Новые поля для старой строки должны быть NULL
        assert row[2] is None
        assert row[3] is None
        assert row[4] is None

        # Проверяем наличие новых индексов
        idx_names = {r[1] for r in db.execute("PRAGMA index_list(price_history)").fetchall()}
        assert "ix_price_route_obs" in idx_names
        assert "ix_price_search_id" in idx_names

    # Повторное открытие должно быть безопасным и идемпотентным
    h2 = PriceHistory(db_path)
    with h2._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        assert count == 1


def test_annotate_populates_new_fields_and_search_id(tmp_path):
    """annotate корректно рассчитывает и записывает новые поля Offer."""
    h = PriceHistory(tmp_path / "h.sqlite")
    # Базовая дата наблюдения
    now_ts = datetime(2026, 11, 20, 12, 0, 0).timestamp()

    o1 = Offer(
        site="s1",
        price=10000,
        offer_id="o1",
        baggage="Тариф с багажом 23 кг",
        airline="Aeroflot",
        flight_numbers=["SU100", "SU101"],
        depart_at="2026-12-01T08:30:00",
    )
    o2 = Offer(
        site="s2",
        price=8500,
        offer_id="o2",
        baggage="Только ручная кладь, без багажа",
        airline="Pobeda",
        flight_numbers=["DP505"],
        depart_time="14:00",
    )
    o3 = Offer(
        site="s1",
        price=12000,
        offer_id="o3",
        baggage=None,
        airline=None,
        flight_numbers=None,
    )

    h.annotate(Q, _make_res([o1, o2, o3]), now=now_ts)

    with h._connect() as db:
        rows = db.execute(
            "SELECT offer_id, days_to_departure, baggage, airline, flight_numbers, depart_at, search_id "
            "FROM price_history ORDER BY id ASC"
        ).fetchall()
        assert len(rows) == 3

        # days_to_departure: 2026-12-01 - 2026-11-20 = 11 дней
        assert rows[0][0] == "o1"
        assert rows[0][1] == 11
        assert rows[0][2] == "with"
        assert rows[0][3] == "Aeroflot"
        assert rows[0][4] == "SU100,SU101"
        assert rows[0][5] == "2026-12-01T08:30:00"
        sid1 = rows[0][6]
        assert sid1 and len(sid1) <= 12

        assert rows[1][0] == "o2"
        assert rows[1][1] == 11
        assert rows[1][2] == "without"
        assert rows[1][3] == "Pobeda"
        assert rows[1][4] == "DP505"
        assert rows[1][5] == "14:00"
        sid2 = rows[1][6]

        assert rows[2][0] == "o3"
        assert rows[2][1] == 11
        assert rows[2][2] is None
        assert rows[2][3] is None
        assert rows[2][4] is None
        assert rows[2][5] is None
        sid3 = rows[2][6]

        # search_id общий для всех записей одного вызова annotate
        assert sid1 == sid2 == sid3

    # Проверка передачи явного search_id
    h.annotate(
        Q,
        _make_res([Offer(site="s1", price=9999, offer_id="o4")]),
        now=now_ts + 700,
        search_id="custom_sid_123",
    )
    with h._connect() as db:
        saved_sid = db.execute("SELECT search_id FROM price_history WHERE offer_id = 'o4'").fetchone()[0]
        assert saved_sid == "custom_sid_123"


def test_rebuild_route_daily_calculation_and_idempotence(tmp_path):
    """Сводки route_daily строятся по лучшей цене каждого замера и устойчивы к повторам."""
    h = PriceHistory(tmp_path / "h.sqlite")
    target_day = date(2026, 11, 15)
    t0 = datetime.combine(target_day, datetime.min.time()).replace(hour=10).timestamp()

    # Замер 1 (search_id = 's1'): 2 билета с ценами 10000 и 15000 -> лучшая цена 10000
    h.annotate(
        Q,
        _make_res([
            Offer(site="s1", price=10000, offer_id="off1"),
            Offer(site="s2", price=15000, offer_id="off2"),
        ]),
        now=t0,
        search_id="s1",
    )

    # Замер 2 (search_id = 's2'): лучшая цена 12000
    h.annotate(
        Q,
        _make_res([
            Offer(site="s1", price=12000, offer_id="off3"),
            Offer(site="s2", price=14000, offer_id="off4"),
        ]),
        now=t0 + 3600,
        search_id="s2",
    )

    # Замер 3 (search_id = 's3'): лучшая цена 14000
    h.annotate(
        Q,
        _make_res([Offer(site="s1", price=14000, offer_id="off5")]),
        now=t0 + 7200,
        search_id="s3",
    )

    # Замер 4 (search_id = 's4'): лучшая цена 20000
    h.annotate(
        Q,
        _make_res([Offer(site="s1", price=20000, offer_id="off6")]),
        now=t0 + 10800,
        search_id="s4",
    )

    # Пересчитываем сводку за target_day
    written = h.rebuild_route_daily(target_day)
    assert written == 1

    best_prices = [10000, 12000, 14000, 20000]
    expected_min = 10000
    expected_median = float(statistics.median(best_prices))
    expected_p25 = float(statistics.quantiles(best_prices, n=4)[0])

    with h._connect() as db:
        row = db.execute(
            "SELECT origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples "
            "FROM route_daily WHERE day = ?",
            (target_day.isoformat(),),
        ).fetchone()
        assert row is not None
        assert row[0:5] == ("KUF", "EVN", "2026-12-01", "", 1)
        assert row[5] == target_day.isoformat()
        assert row[6] == expected_min
        assert row[7] == pytest.approx(expected_p25)
        assert row[8] == pytest.approx(expected_median)
        assert row[9] == 4  # 4 замера, а не 6 билетов

    # Идемпотентность: повторный запуск переписывает то же самое
    written_again = h.rebuild_route_daily(target_day)
    assert written_again == 1
    with h._connect() as db:
        total_daily_rows = db.execute("SELECT COUNT(*) FROM route_daily").fetchone()[0]
        assert total_daily_rows == 1


def test_rebuild_route_daily_legacy_records_grouped_by_minute(tmp_path):
    """Старые записи без search_id группируются до минуты site-независимо."""
    h = PriceHistory(tmp_path / "h.sqlite")
    d = date(2026, 11, 10)
    # В одну и ту же минуту (10:00:10 и 10:00:40) два сайта вернули цены
    t_min1_a = datetime.combine(d, datetime.min.time()).replace(hour=10, minute=0, second=5).timestamp()
    t_min1_b = datetime.combine(d, datetime.min.time()).replace(hour=10, minute=0, second=15).timestamp()

    # Через 30 минут другой замер
    t_min2 = datetime.combine(d, datetime.min.time()).replace(hour=10, minute=30, second=0).timestamp()

    with h._connect() as db:
        # Вставляем старые записи без search_id
        db.execute(
            "INSERT INTO price_history "
            "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, search_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            ("leg1", "siteA", "KUF", "EVN", "2026-12-01", None, 1, 11000, "RUB", t_min1_a),
        )
        db.execute(
            "INSERT INTO price_history "
            "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, search_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            ("leg2", "siteB", "KUF", "EVN", "2026-12-01", None, 1, 9500, "RUB", t_min1_b),
        )
        db.execute(
            "INSERT INTO price_history "
            "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, search_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            ("leg3", "siteA", "KUF", "EVN", "2026-12-01", None, 1, 12000, "RUB", t_min2),
        )

    written = h.rebuild_route_daily(d)
    assert written == 1

    with h._connect() as db:
        row = db.execute("SELECT samples, min_price, median_price FROM route_daily WHERE day = ?", (d.isoformat(),)).fetchone()
        # Замеры: минута 1 (лучшая цена 9500), минута 2 (лучшая цена 12000) -> samples = 2
        assert row[0] == 2
        assert row[1] == 9500
        assert row[2] == pytest.approx(10750.0)


def test_rebuild_route_daily_default_yesterday_and_today(tmp_path):
    """rebuild_route_daily() без аргументов пересчитывает сводки за вчера и сегодня."""
    h = PriceHistory(tmp_path / "h.sqlite")
    today = date.today()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=5)

    t_today = datetime.combine(today, datetime.min.time()).replace(hour=12, minute=0).timestamp()
    t_yesterday = datetime.combine(yesterday, datetime.min.time()).replace(hour=12, minute=0).timestamp()
    t_day_before = datetime.combine(day_before, datetime.min.time()).replace(hour=12, minute=0).timestamp()

    h.annotate(Q, _make_res([Offer(site="s", price=6500, offer_id="tod1")]), now=t_today)
    h.annotate(Q, _make_res([Offer(site="s", price=7000, offer_id="yest1")]), now=t_yesterday)
    h.annotate(Q, _make_res([Offer(site="s", price=5000, offer_id="old1")]), now=t_day_before)

    written = h.rebuild_route_daily()
    assert written == 2

    with h._connect() as db:
        days_in_daily = {r[0]: r[1] for r in db.execute("SELECT day, min_price FROM route_daily").fetchall()}
        assert yesterday.isoformat() in days_in_daily
        assert today.isoformat() in days_in_daily
        assert day_before.isoformat() not in days_in_daily
        assert days_in_daily[yesterday.isoformat()] == 7000
        assert days_in_daily[today.isoformat()] == 6500


def test_prune_rolls_up_and_deletes_raw_records(tmp_path):
    """prune сворачивает дни старше keep_days в route_daily и удаляет только сырые price_history."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = datetime(2026, 11, 20, 12, 0, 0).timestamp()

    # День 60 дней назад (старше 45 дней)
    t_old = now_ts - 60 * 86400
    d_old = datetime.fromtimestamp(t_old).date()

    # День 10 дней назад (свежий)
    t_recent = now_ts - 10 * 86400

    h.annotate(Q, _make_res([Offer(site="s", price=8000, offer_id="old1")]), now=t_old)
    h.annotate(Q, _make_res([Offer(site="s", price=9000, offer_id="rec1")]), now=t_recent)

    # Проверяем, что в route_daily пока ничего нет
    with h._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM route_daily").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 2

    # Запускаем чистку с keep_days = 45
    deleted = h.prune(keep_days=45, now=now_ts)
    assert deleted == 1

    with h._connect() as db:
        # Старая сырая запись удалена
        raw_rows = db.execute("SELECT offer_id FROM price_history").fetchall()
        assert len(raw_rows) == 1
        assert raw_rows[0][0] == "rec1"

        # Но в route_daily старый день свернулся!
        daily_rows = db.execute("SELECT day, min_price, samples FROM route_daily").fetchall()
        assert len(daily_rows) == 1
        assert daily_rows[0][0] == d_old.isoformat()
        assert daily_rows[0][1] == 8000
        assert daily_rows[0][2] == 1


def test_prune_multiple_routes_on_same_day(tmp_path):
    """prune корректно сворачивает все маршруты дня, даже если один из них уже был в route_daily."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = datetime(2026, 11, 20, 12, 0, 0).timestamp()
    t_old = now_ts - 60 * 86400
    d_old = datetime.fromtimestamp(t_old).date()

    q2 = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    # Записываем замер по первому маршруту и вручную сворачиваем день
    h.annotate(Q, _make_res([Offer(site="s", price=8000, offer_id="route1_old")]), now=t_old)
    h.rebuild_route_daily(d_old)

    with h._connect() as db:
        # День d_old уже есть в route_daily
        assert db.execute("SELECT COUNT(*) FROM route_daily WHERE day = ?", (d_old.isoformat(),)).fetchone()[0] == 1

    # Позже добавляем замер по второму маршруту за тот же старый день
    h.annotate(q2, _make_res([Offer(site="s", price=15000, offer_id="route2_old")]), now=t_old + 60)

    # Вызываем prune: день d_old должен быть пересчитан для всех маршрутов,
    # а не пропущен из-за наличия d_old в route_daily
    deleted = h.prune(keep_days=45, now=now_ts)
    assert deleted == 2  # удалены обе сырые записи

    with h._connect() as db:
        # В route_daily теперь должны быть записи по ОБОИМ маршрутам
        rows = db.execute(
            "SELECT origin, destination, min_price, samples FROM route_daily WHERE day = ? ORDER BY origin",
            (d_old.isoformat(),),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == ("KUF", "EVN", 8000, 1)
        assert rows[1] == ("MOW", "LED", 15000, 1)


def test_coverage_and_heartbeat(tmp_path):
    """record_heartbeat ограничивает размер таблицы, а coverage вычисляет пропуски."""
    h = PriceHistory(tmp_path / "h.sqlite")

    # 1. Проверка ограничения в 5000 записей
    with h._connect() as db:
        # Вставляем 5050 меток
        for i in range(5050):
            db.execute("INSERT INTO scheduler_heartbeat (ts) VALUES (?)", (float(i),))
    # Вызываем record_heartbeat — он должен удалить лишние
    h.record_heartbeat(now=6000.0)

    with h._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM scheduler_heartbeat").fetchone()[0]
        assert count == 5000
        # Самая новая метка на месте
        max_ts = db.execute("SELECT MAX(ts) FROM scheduler_heartbeat").fetchone()[0]
        assert max_ts == 6000.0

    # 2. Проверка вычисления покрытия и пропусков
    h_cov = PriceHistory(tmp_path / "h_cov.sqlite")
    # Допустим, бот работал 1000..1200 (200 сек), потом был выключен 2 часа (7200 сек),
    # потом работал 8400..8600 (200 сек)
    h_cov.record_heartbeat(1000.0)
    h_cov.record_heartbeat(1100.0)
    h_cov.record_heartbeat(1200.0)
    # Пропуск 7200 сек (> 3600 gap_seconds)
    h_cov.record_heartbeat(8400.0)
    h_cov.record_heartbeat(8500.0)
    h_cov.record_heartbeat(8600.0)

    res = h_cov.coverage(since=1000.0, until=8600.0, gap_seconds=3600.0)
    assert len(res["gaps"]) == 1
    assert res["gaps"][0] == (1200.0, 8400.0)
    # Общее время 7600 сек, пропуск 7200 сек -> покрыто 400 сек
    assert res["covered_seconds"] == pytest.approx(400.0)

    # Пустая база
    h_empty = PriceHistory(tmp_path / "h_empty.sqlite")
    res_empty = h_empty.coverage(since=100.0, until=5000.0, gap_seconds=3600.0)
    assert res_empty["covered_seconds"] == 0.0
    assert res_empty["gaps"] == [(100.0, 5000.0)]

    # Без пропусков
    res_no_gap = h_cov.coverage(since=1000.0, until=1200.0, gap_seconds=3600.0)
    assert res_no_gap["gaps"] == []
    assert res_no_gap["covered_seconds"] == pytest.approx(200.0)


def test_coverage_with_only_old_heartbeats(tmp_path):
    """coverage не выходит за границы [since, until], когда heartbeat был только до since."""
    h = PriceHistory(tmp_path / "h.sqlite")
    # Метка планировщика только на 2000
    h.record_heartbeat(2000.0)

    # Запрос покрытия на [5000, 6000] с порогом 3600
    # Промежуток между 2000 и 6000 равен 4000 (> 3600), поэтому весь интервал [5000, 6000] — пропуск
    res = h.coverage(since=5000.0, until=6000.0, gap_seconds=3600.0)
    assert res["gaps"] == [(5000.0, 6000.0)]
    assert res["covered_seconds"] == 0.0

    # Случай, когда heartbeat был незадолго до since (разрыв меньше gap_seconds)
    # Например, heartbeat на 4900, запрос [5000, 6000] (интервал 1100 < 3600)
    h.record_heartbeat(4900.0)
    res_recent = h.coverage(since=5000.0, until=6000.0, gap_seconds=3600.0)
    assert res_recent["gaps"] == []
    assert res_recent["covered_seconds"] == pytest.approx(1000.0)


def test_route_stats(tmp_path):
    """route_stats вычисляет min, p25, median, last_price, percentile_of_last, trends, volatility."""
    h = PriceHistory(tmp_path / "h.sqlite")

    # Меньше 3 замеров -> None
    assert h.route_stats(Q) is None
    h.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="o1")]), now=1000.0)
    assert h.route_stats(Q) is None
    h.annotate(Q, _make_res([Offer(site="s", price=11000, offer_id="o2")]), now=2000.0)
    assert h.route_stats(Q) is None

    # Добавляем замеры по дням для проверки тренда
    # День 0: 10000
    # День 1: 10000
    # День 2: 10000
    # День 3: 11000 (+10% относительно 3 дней назад -> up)
    # День 7: 12000 (+20% относительно 7 дней назад -> up)
    base_ts = datetime(2026, 11, 1, 12, 0).timestamp()
    h3 = PriceHistory(tmp_path / "h3.sqlite")
    h3.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="m0")]), now=base_ts)
    h3.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="m1")]), now=base_ts + 1 * 86400)
    h3.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="m2")]), now=base_ts + 2 * 86400)
    h3.annotate(Q, _make_res([Offer(site="s", price=11000, offer_id="m3")]), now=base_ts + 3 * 86400)

    stats_3d = h3.route_stats(Q)
    assert stats_3d is not None
    assert stats_3d["samples"] == 4
    assert stats_3d["last_price"] == 11000
    assert stats_3d["min"] == 10000
    assert stats_3d["trend_3d"] == "up"
    assert stats_3d["trend_7d"] is None  # 7 дней истории пока нет

    # Добавляем день 7 с ценой 12000
    h3.annotate(Q, _make_res([Offer(site="s", price=12000, offer_id="m7")]), now=base_ts + 7 * 86400)
    stats_7d = h3.route_stats(Q)
    assert stats_7d["samples"] == 5
    assert stats_7d["last_price"] == 12000
    assert stats_7d["trend_7d"] == "up"

    # Проверка доли замеров дороже последнего:
    # Замеры: 10000, 10000, 10000, 11000, 12000.
    # Последняя цена 12000: дороже её 0 замеров -> 0.0%
    assert stats_7d["percentile_of_last"] == 0.0

    # Проверяем, когда последний замер самый дешёвый:
    # Добавляем замер 9000
    h3.annotate(Q, _make_res([Offer(site="s", price=9000, offer_id="m8")]), now=base_ts + 7 * 86400 + 1000)
    stats_cheap = h3.route_stats(Q)
    # Цены: 10000, 10000, 10000, 11000, 12000, 9000 (6 замеров)
    # Дороже 9000 все 5 предыдущих -> 5/6 * 100 = 83.3%
    assert stats_cheap["percentile_of_last"] == pytest.approx(83.3, abs=0.1)
    assert stats_cheap["min"] == 9000

    # Проверка flat тренда (изменение < 2%)
    h_flat = PriceHistory(tmp_path / "h_flat.sqlite")
    h_flat.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="f0")]), now=base_ts)
    h_flat.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="f1")]), now=base_ts + 86400)
    h_flat.annotate(Q, _make_res([Offer(site="s", price=10100, offer_id="f2")]), now=base_ts + 3 * 86400)
    stats_flat = h_flat.route_stats(Q)
    assert stats_flat["trend_3d"] == "flat"

    # Проверка down тренда (изменение < -2%)
    h_down = PriceHistory(tmp_path / "h_down.sqlite")
    h_down.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="d0")]), now=base_ts)
    h_down.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="d1")]), now=base_ts + 86400)
    h_down.annotate(Q, _make_res([Offer(site="s", price=9500, offer_id="d2")]), now=base_ts + 3 * 86400)
    stats_down = h_down.route_stats(Q)
    assert stats_down["trend_3d"] == "down"

    # Волатильность
    prices = [10000, 10000, 9500]
    expected_volatility = round((statistics.stdev(prices) / statistics.mean(prices)) * 100.0, 1)
    assert stats_down["volatility_pct"] == pytest.approx(expected_volatility, abs=0.1)


def test_route_stats_with_round_trip_and_pruned_daily(tmp_path):
    """route_stats корректно работает для маршрутов туда-обратно и объединяет route_daily со свежими сырыми."""
    h = PriceHistory(tmp_path / "h_rt.sqlite")
    base_ts = datetime(2026, 11, 1, 12, 0).timestamp()

    # Сначала пишем данные 60 дней назад
    t_old = base_ts - 60 * 86400
    h.annotate(Q_ROUND, _make_res([Offer(site="s", price=25000, offer_id="rt_old")]), now=t_old)

    # Сворачиваем старые данные через prune
    h.prune(keep_days=30, now=base_ts)

    # Добавляем 2 свежих замера
    h.annotate(Q_ROUND, _make_res([Offer(site="s", price=24000, offer_id="rt_rec1")]), now=base_ts - 86400)
    h.annotate(Q_ROUND, _make_res([Offer(site="s", price=22000, offer_id="rt_rec2")]), now=base_ts)

    stats = h.route_stats(Q_ROUND)
    assert stats is not None
    # 1 свернутый замер + 2 свежих = 3 замера
    assert stats["samples"] == 3
    assert stats["min"] == 22000
    assert stats["last_price"] == 22000
    # Дороже 22000 оба других замера (25000 и 24000) -> 2/3 * 100 = 66.7%
    assert stats["percentile_of_last"] == pytest.approx(66.7, abs=0.1)


def test_prune_aligns_to_calendar_day_and_prevents_partial_day_corruption(tmp_path):
    """prune выравнивает границу по началу календарного дня и не повреждает данные частичным удалением."""
    h = PriceHistory(tmp_path / "h_prune.sqlite")
    now_ts = datetime(2026, 11, 20, 14, 0, 0).timestamp()

    # d_46 (46 дней назад): 2026-10-05
    d_46 = date(2026, 11, 20) - timedelta(days=46)
    t_46_morning = datetime.combine(d_46, datetime.min.time()).replace(hour=10).timestamp()
    t_46_evening = datetime.combine(d_46, datetime.min.time()).replace(hour=18).timestamp()

    # d_45 (45 дней назад): 2026-10-06
    d_45 = date(2026, 11, 20) - timedelta(days=45)
    t_45_morning = datetime.combine(d_45, datetime.min.time()).replace(hour=10).timestamp()
    t_45_evening = datetime.combine(d_45, datetime.min.time()).replace(hour=18).timestamp()

    h.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="d46_m")]), now=t_46_morning)
    h.annotate(Q, _make_res([Offer(site="s", price=8000, offer_id="d46_e")]), now=t_46_evening)
    h.annotate(Q, _make_res([Offer(site="s", price=9000, offer_id="d45_m")]), now=t_45_morning)
    h.annotate(Q, _make_res([Offer(site="s", price=7000, offer_id="d45_e")]), now=t_45_evening)

    # Запуск чистки в 14:00 с keep_days=45:
    # Граница чистки: начало дня 2026-10-06 (00:00:00).
    # d_45 не должен удаляться вовсе (ни утренние, ни вечерние записи).
    # d_46 целиком старше границы: он сворачивается в route_daily и удаляется из price_history.
    deleted = h.prune(keep_days=45, now=now_ts)
    assert deleted == 2

    with h._connect() as db:
        # В price_history остались ОБЕ записи за d_45 (10:00 и 18:00)
        remaining = db.execute("SELECT offer_id FROM price_history ORDER BY observed_at").fetchall()
        assert [r[0] for r in remaining] == ["d45_m", "d45_e"]

        # В route_daily свернулся день d_46 с полной статистикой (2 замера, мин. цена 8000)
        daily_d46 = db.execute("SELECT samples, min_price FROM route_daily WHERE day = ?", (d_46.isoformat(),)).fetchone()
        assert daily_d46 == (2, 8000)

    # На следующий день (2026-11-21 14:00) снова запускаем prune
    next_day_ts = datetime(2026, 11, 21, 14, 0, 0).timestamp()
    deleted_next = h.prune(keep_days=45, now=next_day_ts)
    assert deleted_next == 2

    with h._connect() as db:
        # Сырых записей больше нет
        assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0

        # В route_daily теперь есть и d_45 с полной статистикой (2 замера, мин. цена 7000)
        daily_d45 = db.execute("SELECT samples, min_price FROM route_daily WHERE day = ?", (d_45.isoformat(),)).fetchone()
        assert daily_d45 == (2, 7000)

        # d_46 остался без изменений
        daily_d46_after = db.execute("SELECT samples, min_price FROM route_daily WHERE day = ?", (d_46.isoformat(),)).fetchone()
        assert daily_d46_after == (2, 8000)


def test_route_stats_uses_p25_and_preserves_distribution(tmp_path):
    """route_stats использует p25_price из route_daily при моделировании распределения."""
    h = PriceHistory(tmp_path / "h_p25.sqlite")
    d_str = "2026-09-01"

    # Создаём сводку дня в route_daily: min=10000, p25=12000, median=20000, samples=4
    with h._connect() as db:
        db.execute(
            "INSERT INTO route_daily "
            "(origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("KUF", "EVN", "2026-12-01", "", 1, d_str, 10000, 12000.0, 20000.0, 4),
        )

    # Добавляем один свежий замер в сырые записи
    now_ts = datetime(2026, 11, 20, 12, 0, 0).timestamp()
    h.annotate(Q, _make_res([Offer(site="s", price=11000, offer_id="rec1")]), now=now_ts)

    stats = h.route_stats(Q)
    assert stats is not None
    assert stats["samples"] == 5
    assert stats["min"] == 10000
    # p25 должен отражать значение p25 (12000), а не вырождаться в медиану (20000)
    assert stats["p25"] < 15000
    # Волатильность не занижена до нуля
    assert stats["volatility_pct"] is not None and stats["volatility_pct"] > 10.0


def test_trend_requires_sufficient_history_span(tmp_path):
    """_trend возвращает None, если охват истории недостаточен (меньше ~2.5 суток для 3d-тренда)."""
    h = PriceHistory(tmp_path / "h_trend.sqlite")
    now_ts = datetime(2026, 11, 20, 14, 0, 0).timestamp()

    # Замеры только за вчера (30 часов назад) и сегодня: охват ~1.25 суток
    t_yesterday = now_ts - 30 * 3600
    h.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="y1")]), now=t_yesterday)
    h.annotate(Q, _make_res([Offer(site="s", price=10500, offer_id="y2")]), now=t_yesterday + 600)
    h.annotate(Q, _make_res([Offer(site="s", price=15000, offer_id="tod")]), now=now_ts)

    stats = h.route_stats(Q)
    assert stats is not None
    assert stats["samples"] == 3
    # Для 3d-тренда требуется охват не менее 3 * 0.8 = 2.4 суток. При 1.25 сутках trend_3d должен быть None!
    assert stats["trend_3d"] is None

    # Добавляем замер ровно 3 дня назад
    t_3d = now_ts - 3 * 86400
    h.annotate(Q, _make_res([Offer(site="s", price=10000, offer_id="old_3d")]), now=t_3d)

    stats_with_3d = h.route_stats(Q)
    assert stats_with_3d is not None
    assert stats_with_3d["trend_3d"] == "up"


def test_annotate_and_route_stats_accept_datetime_and_strings(tmp_path):
    """annotate и route_stats корректно работают с объектами datetime и строками в depart/return_date."""
    h = PriceHistory(tmp_path / "h_types.sqlite")
    now_ts = datetime(2026, 11, 20, 12, 0, 0).timestamp()

    # Query с объектом datetime в depart и строкой в return_date
    q_dt = Query(
        origin="KUF",
        destination="EVN",
        depart=datetime(2026, 12, 1, 15, 30),  # datetime!
        return_date="2026-12-10",  # str!
    )

    o = Offer(
        site="s",
        price=10000,
        offer_id="o_dt",
        baggage="С багажом",
    )

    # Не должно упасть с TypeError или AttributeError
    h.annotate(q_dt, _make_res([o]), now=now_ts)

    with h._connect() as db:
        row = db.execute(
            "SELECT depart, return_date, days_to_departure FROM price_history WHERE offer_id = 'o_dt'"
        ).fetchone()
        assert row is not None
        assert row[0] == "2026-12-01"
        assert row[1] == "2026-12-10"
        # 2026-12-01 - 2026-11-20 = 11 дней
        assert row[2] == 11

    # Добавим ещё 2 замера, чтобы проверить route_stats
    h.annotate(q_dt, _make_res([Offer(site="s", price=9500, offer_id="o_dt2")]), now=now_ts + 3600)
    h.annotate(q_dt, _make_res([Offer(site="s", price=9000, offer_id="o_dt3")]), now=now_ts + 7200)

    stats = h.route_stats(q_dt)
    assert stats is not None
    assert stats["samples"] == 3
    assert stats["min"] == 9000
    assert stats["last_price"] == 9000



def test_prune_keeps_late_samples_of_partially_rolled_day(tmp_path):
    """День, уже частично свёрнутый в route_daily, досворачивается перед удалением сырых записей."""
    import sqlite3
    import time
    from datetime import datetime, timedelta

    from avia.history import PriceHistory

    h = PriceHistory(tmp_path / "h.sqlite")
    now = time.time()
    old_day = (datetime.fromtimestamp(now) - timedelta(days=60)).replace(hour=9, minute=0, second=0).timestamp()

    def put(ts, price, sid):
        with sqlite3.connect(h.path) as db:
            db.execute(
                "INSERT INTO price_history (offer_id, site, origin, destination, depart, return_date, adults, "
                "price, currency, observed_at, search_id) VALUES ('o','s','KUF','EVN','2030-12-01',NULL,1,?,?,?,?)",
                (price, "RUB", ts, sid),
            )

    put(old_day, 10000, "a")
    h.rebuild_route_daily(datetime.fromtimestamp(old_day).date())
    put(old_day + 8 * 3600, 5000, "b")  # поздний замер уже после первой свёртки
    h.prune(keep_days=45, now=now)
    with sqlite3.connect(h.path) as db:
        mn, samples = db.execute("SELECT min_price, samples FROM route_daily").fetchone()
    assert (mn, samples) == (5000, 2)


def test_coverage_gap_clamped_and_future_mark_not_coverage(tmp_path):
    from avia.history import PriceHistory

    h = PriceHistory(tmp_path / "h.sqlite")
    h.record_heartbeat(10000)
    r = h.coverage(0, 5000, gap_seconds=3600)
    assert r["gaps"] == [(0.0, 5000.0)] and r["covered_seconds"] == 0.0
    r = h.coverage(8000, 9000, gap_seconds=3600)
    assert r["gaps"] == [(8000.0, 9000.0)] and r["covered_seconds"] == 0.0


def test_rebuild_route_daily_accepts_datetime_and_str(tmp_path):
    from datetime import datetime

    from avia.history import PriceHistory

    h = PriceHistory(tmp_path / "h.sqlite")
    assert h.rebuild_route_daily(datetime(2026, 1, 1, 12, 0)) == 0
    assert h.rebuild_route_daily("2026-01-01") == 0


def test_route_series_raw_and_daily_combination(tmp_path):
    """route_series возвращает хронологические точки цен из raw и свёрнутых route_daily."""
    h = PriceHistory(tmp_path / "h.sqlite")
    base_dt = datetime(2026, 11, 20, 12, 0, 0)
    now_ts = base_dt.timestamp()

    # Вставляем старый день в route_daily (10 дней назад, 2026-11-10)
    old_date = (base_dt - timedelta(days=10)).date().isoformat()
    with sqlite3.connect(h.path) as db:
        db.execute(
            "INSERT INTO route_daily (origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples) "
            "VALUES ('KUF', 'EVN', '2026-12-01', '', 1, ?, 9000, 9500.0, 10000.0, 5)",
            (old_date,),
        )

    # Вставляем свежие замеры в price_history (сегодня и вчера)
    # Замер 1: два предложения в одном search_id (12 000 и 11 000) -> минимум 11 000
    h.annotate(
        Q,
        _make_res([
            Offer(site="s1", price=12000, offer_id="o1"),
            Offer(site="s2", price=11000, offer_id="o2"),
        ]),
        now=now_ts - 86400,
        search_id="search_1",
    )
    # Замер 2: один билет на 10 500
    h.annotate(
        Q,
        _make_res([Offer(site="s1", price=10500, offer_id="o3")]),
        now=now_ts,
        search_id="search_2",
    )

    series = h.route_series(Q, days=30, now=now_ts)
    # Должно быть 3 точки: 1 из route_daily (медиана 10000), 2 из сырых (11000 и 10500)
    assert len(series) == 3
    # Проверяем хронологическую сортировку
    assert series[0][1] == 10000  # день из route_daily
    assert series[1][1] == 11000  # минимум search_1
    assert series[2][1] == 10500  # минимум search_2
    assert series[0][0] < series[1][0] < series[2][0]

    # Если день есть в сырых записях, он не дублируется из route_daily
    today_date = base_dt.date().isoformat()
    with sqlite3.connect(h.path) as db:
        db.execute(
            "INSERT INTO route_daily (origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples) "
            "VALUES ('KUF', 'EVN', '2026-12-01', '', 1, ?, 7000, 7000.0, 7000.0, 1)",
            (today_date,),
        )
    series_dedup = h.route_series(Q, days=30, now=now_ts)
    assert len(series_dedup) == 3


def test_route_series_fallback_minute_grouping_and_roundtrip(tmp_path):
    """route_series группирует записи без search_id по минуте и учитывает return_date."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = 1700000000.0

    # Записи без search_id в одну минуту
    with sqlite3.connect(h.path) as db:
        db.execute(
            "INSERT INTO price_history (offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, search_id) "
            "VALUES ('o1', 's1', 'KUF', 'EVN', '2026-12-01', '2026-12-10', 1, 20000, 'RUB', ?, NULL)",
            (now_ts,),
        )
        db.execute(
            "INSERT INTO price_history (offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, search_id) "
            "VALUES ('o2', 's2', 'KUF', 'EVN', '2026-12-01', '2026-12-10', 1, 18500, 'RUB', ?, NULL)",
            (now_ts + 3,),
        )

    # Для Q_ROUND должен найтись минимум 18 500
    series = h.route_series(Q_ROUND, days=5, now=now_ts + 60)
    assert len(series) == 1
    assert series[0][1] == 18500

    # Для Q (в одну сторону) записей быть не должно
    assert h.route_series(Q, days=5, now=now_ts + 60) == []


def test_route_search_minima(tmp_path):
    """route_search_minima возвращает хронологический список минимумов за период."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = 100000.0

    # Добавляем 3 замера
    h.annotate(Q, _make_res([Offer(site="s", price=15000, offer_id="o1")]), now=now_ts, search_id="s1")
    h.annotate(
        Q,
        _make_res([Offer(site="s", price=14000, offer_id="o2"), Offer(site="s", price=13000, offer_id="o3")]),
        now=now_ts + 1000,
        search_id="s2",
    )
    h.annotate(Q, _make_res([Offer(site="s", price=16000, offer_id="o4")]), now=now_ts + 2000, search_id="s3")

    # Полный период
    minima = h.route_search_minima(Q, since_ts=now_ts - 100)
    assert minima == [15000, 13000, 16000]

    # С ограничением until_ts
    minima_limited = h.route_search_minima(Q, since_ts=now_ts - 100, until_ts=now_ts + 1500)
    assert minima_limited == [15000, 13000]


def test_cheapest_site_stats(tmp_path):
    """cheapest_site_stats подсчитывает число побед и участий каждого сайта по убыванию побед."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = 200000.0

    # Замер 1: aviasales 10000 (победа), kupibilet 10500, tutu 12000
    h.annotate(
        Q,
        [
            SiteResult(site="aviasales", ok=True, offers=[Offer(site="aviasales", price=10000, offer_id="o1")]),
            SiteResult(site="kupibilet", ok=True, offers=[Offer(site="kupibilet", price=10500, offer_id="o2")]),
            SiteResult(site="tutu", ok=True, offers=[Offer(site="tutu", price=12000, offer_id="o3")]),
        ],
        now=now_ts,
        search_id="s1",
    )

    # Замер 2: aviasales 11000, kupibilet 9500 (победа)
    h.annotate(
        Q,
        [
            SiteResult(site="aviasales", ok=True, offers=[Offer(site="aviasales", price=11000, offer_id="o4")]),
            SiteResult(site="kupibilet", ok=True, offers=[Offer(site="kupibilet", price=9500, offer_id="o5")]),
        ],
        now=now_ts + 300,
        search_id="s2",
    )

    # Замер 3 (ничья): aviasales 9000 (победа), kupibilet 9000 (победа)
    h.annotate(
        Q,
        [
            SiteResult(site="aviasales", ok=True, offers=[Offer(site="aviasales", price=9000, offer_id="o6")]),
            SiteResult(site="kupibilet", ok=True, offers=[Offer(site="kupibilet", price=9000, offer_id="o7")]),
        ],
        now=now_ts + 600,
        search_id="s3",
    )

    stats = h.cheapest_site_stats(Q, days=7, now=now_ts + 1000)
    # kupibilet: 2 победы из 3 замеров
    # aviasales: 2 победы из 3 замеров
    # tutu: 0 побед из 1 замера
    assert len(stats) == 3
    site_dict = {site: (wins, total) for site, wins, total in stats}
    assert site_dict["kupibilet"] == (2, 3)
    assert site_dict["aviasales"] == (2, 3)
    assert site_dict["tutu"] == (0, 1)
    # tutu должен быть последним (0 побед)
    assert stats[-1][0] == "tutu"


def test_popular_routes(tmp_path):
    """popular_routes возвращает маршруты по числу различных замеров за период."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = 500000.0

    # Маршрут 1: KUF -> EVN (3 замера)
    h.annotate(Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1)), _make_res([Offer(site="s", price=10000, offer_id="o1")]), now=now_ts, search_id="sr1")
    h.annotate(Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1)), _make_res([Offer(site="s", price=10000, offer_id="o2")]), now=now_ts + 100, search_id="sr2")
    h.annotate(Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1)), _make_res([Offer(site="s", price=10000, offer_id="o3")]), now=now_ts + 200, search_id="sr3")

    # Маршрут 2: MOW -> AER (2 замера, но во 2-м замере 2 билета - должен считаться за 1 замер)
    h.annotate(Query(origin="MOW", destination="AER", depart=date(2026, 12, 5)), _make_res([Offer(site="s", price=5000, offer_id="o4")]), now=now_ts + 300, search_id="sr4")
    h.annotate(
        Query(origin="MOW", destination="AER", depart=date(2026, 12, 5)),
        _make_res([Offer(site="s", price=4900, offer_id="o5"), Offer(site="s", price=5100, offer_id="o6")]),
        now=now_ts + 400,
        search_id="sr5",
    )

    # Маршрут 3: LED -> DXB (1 замер)
    h.annotate(Query(origin="LED", destination="DXB", depart=date(2026, 12, 10)), _make_res([Offer(site="s", price=25000, offer_id="o7")]), now=now_ts + 500, search_id="sr6")

    popular = h.popular_routes(limit=2, days=7, now=now_ts + 1000)
    assert len(popular) == 2
    assert popular[0] == ("KUF", "EVN", 3)
    assert popular[1] == ("MOW", "AER", 2)


def test_price_curve_multi_route_and_baskets(tmp_path):
    """price_curve рассчитывает медианы отношений по корзинам дней до вылета по всем маршрутам."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = datetime(2026, 11, 20, 12, 0).timestamp()

    # Маршрут 1: вылет через 5 дней (0-7), 10 дней (8-14), 25 дней (22-30)
    q1 = Query(origin="MOW", destination="LED", depart=date(2026, 11, 25))
    h.annotate(q1, _make_res([Offer("s", price=12000, offer_id="o1")]), now=now_ts, search_id="s1")

    q1_10 = Query(origin="MOW", destination="LED", depart=date(2026, 11, 30))
    h.annotate(q1_10, _make_res([Offer("s", price=10000, offer_id="o2")]), now=now_ts, search_id="s2")

    # Маршрут 2: вылет через 6 дней (0-7), 12 дней (8-14)
    q2 = Query(origin="KUF", destination="EVN", depart=date(2026, 11, 26))
    h.annotate(q2, _make_res([Offer("s", price=24000, offer_id="o3")]), now=now_ts, search_id="s3")

    q2_12 = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 2))
    h.annotate(q2_12, _make_res([Offer("s", price=20000, offer_id="o4")]), now=now_ts, search_id="s4")

    # С min_samples=1 должны вернуться обе корзины "0-7" и "8-14"
    curve = h.price_curve(days=30, now=now_ts + 60, min_samples=1)
    assert "0-7" in curve
    assert "8-14" in curve
    assert curve["0-7"]["samples"] == 2
    assert curve["8-14"]["samples"] == 2


def test_collect_candidates_filters_and_sorting(tmp_path):
    """collect_candidates фильтрует по depart, seen_days, recheck_seconds и сортирует по популярности/давности."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = datetime(2026, 11, 20, 12, 0).timestamp()
    today = date(2026, 11, 20)

    # 1. Маршрут в прошлом (depart < today): должен быть исключён
    q_past = Query(origin="MOW", destination="AER", depart=date(2026, 11, 19))
    h.annotate(q_past, _make_res([Offer("s", 5000, offer_id="past1")]), now=now_ts - 86400, search_id="spast")

    # 2. Маршрут старше seen_days (14 дней): должен быть исключён
    q_old = Query(origin="MOW", destination="KZN", depart=date(2026, 12, 1))
    h.annotate(q_old, _make_res([Offer("s", 4000, offer_id="old1")]), now=now_ts - 15 * 86400, search_id="sold")

    # 3. Маршрут слишком свежий (< recheck_seconds = 6ч): должен быть исключён
    q_fresh = Query(origin="MOW", destination="OVB", depart=date(2026, 12, 5))
    h.annotate(q_fresh, _make_res([Offer("s", 9000, offer_id="fresh1")]), now=now_ts - 2 * 3600, search_id="sfresh")

    # 4. Валидный маршрут A: 1 поиск, проверен 10 часов назад
    q_a = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    h.annotate(q_a, _make_res([Offer("s", 3000, offer_id="a1")]), now=now_ts - 10 * 3600, search_id="sa1")

    # 5. Валидный маршрут B: 3 поиска, последний проверен 8 часов назад (более популярный, чем A)
    q_b = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 10), return_date=date(2026, 12, 20), adults=2)
    h.annotate(q_b, _make_res([Offer("s", 15000, offer_id="b1")]), now=now_ts - 20 * 3600, search_id="sb1")
    h.annotate(q_b, _make_res([Offer("s", 16000, offer_id="b2")]), now=now_ts - 15 * 3600, search_id="sb2")
    h.annotate(q_b, _make_res([Offer("s", 15500, offer_id="b3")]), now=now_ts - 8 * 3600, search_id="sb3")

    # 6. Валидный маршрут C: 1 поиск, проверен 20 часов назад (такая же популярность как A, но обновлялся давнее)
    q_c = Query(origin="SVX", destination="AER", depart=date(2026, 12, 15))
    h.annotate(q_c, _make_res([Offer("s", 7000, offer_id="c1")]), now=now_ts - 20 * 3600, search_id="sc1")

    # Запрашиваем кандидатов
    candidates = h.collect_candidates(limit=5, today=today, now=now_ts, recheck_seconds=6 * 3600, seen_days=14)

    # Проверяем, что невалидные исключены
    destinations = [c.destination for c in candidates]
    assert "AER" not in destinations or "MOW" not in [c.origin for c in candidates if c.destination == "AER"]
    assert "KZN" not in destinations
    assert "OVB" not in destinations

    # Ожидаемый порядок:
    # 1-й: B (3 поиска)
    # 2-й: C (1 поиск, last_obs 20ч назад)
    # 3-й: A (1 поиск, last_obs 10ч назад)
    assert len(candidates) == 3
    assert candidates[0].destination == "EVN"
    assert candidates[0].origin == "KUF"
    assert candidates[0].return_date == date(2026, 12, 20)
    assert candidates[0].adults == 2
    assert candidates[0].currency == "RUB"

    assert candidates[1].destination == "AER"
    assert candidates[1].origin == "SVX"

    assert candidates[2].destination == "LED"
    assert candidates[2].origin == "MOW"

    # Проверяем limit
    limited = h.collect_candidates(limit=1, today=today, now=now_ts, recheck_seconds=6 * 3600, seen_days=14)
    assert len(limited) == 1
    assert limited[0].destination == "EVN"


def test_count_searches_since(tmp_path):
    """count_searches_since считает уникальные замеры (по search_id и минутам) с указанной отсечки."""
    h = PriceHistory(tmp_path / "h.sqlite")
    now_ts = 100000.0
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    # Поиск 1: 2 билета с search_id="s1"
    h.annotate(
        q,
        _make_res([Offer("s", 3000, offer_id="o1"), Offer("s", 3200, offer_id="o2")]),
        now=now_ts - 500,
        search_id="s1",
    )

    # Поиск 2: search_id="s2"
    h.annotate(q, _make_res([Offer("s", 3100, offer_id="o3")]), now=now_ts - 300, search_id="s2")

    # Поиск 3: без search_id (группировка по минуте)
    h.annotate(q, _make_res([Offer("s", 3300, offer_id="o4")]), now=now_ts - 100, search_id=None)

    # Поиск 4: старый (до отсечки)
    h.annotate(q, _make_res([Offer("s", 3000, offer_id="o0")]), now=now_ts - 2000, search_id="sold")

    assert h.count_searches_since(now_ts - 1000) == 3
    assert h.count_searches_since(now_ts - 400) == 2
    assert h.count_searches_since(now_ts - 50) == 0
    assert h.count_searches_since(0) == 4


