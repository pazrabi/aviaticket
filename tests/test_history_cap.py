from datetime import date
import random

from avia.config import Settings, load_settings
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult

Q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))


def test_synthetic_100_and_10_offers_cap_15(tmp_path):
    """На синтетической выдаче из 100 вариантов одного сайта и 10 другого

    в историю попадает 15 и 10 самых дешёвых.
    """
    h = PriceHistory(tmp_path / "h.sqlite")

    # 100 вариантов сайта A с ценами 1000..100000 в случайном порядке
    prices_a = list(range(1000, 101000, 1000))
    random.seed(42)
    shuffled_prices_a = list(prices_a)
    random.shuffle(shuffled_prices_a)
    offers_a = [
        Offer(site="site_a", price=p, offer_id=f"a_{p}")
        for p in shuffled_prices_a
    ]

    # 10 вариантов сайта B с ценами 5000..14000
    offers_b = [
        Offer(site="site_b", price=p, offer_id=f"b_{p}")
        for p in range(5000, 15000, 1000)
    ]

    res = [
        SiteResult(site="site_a", ok=True, offers=offers_a),
        SiteResult(site="site_b", ok=True, offers=offers_b),
    ]

    h.annotate(Q, res, now=1000.0)

    with h._connect() as db:
        rows_a = db.execute(
            "SELECT price, offer_id FROM price_history WHERE site = 'site_a' ORDER BY price ASC"
        ).fetchall()
        rows_b = db.execute(
            "SELECT price, offer_id FROM price_history WHERE site = 'site_b' ORDER BY price ASC"
        ).fetchall()

    # Ровно 15 самых дешёвых для site_a и все 10 для site_b
    assert len(rows_a) == 15
    assert len(rows_b) == 10

    # 15 самых дешёвых сайта A — это ровно первые 15 минимальных цен
    expected_cheapest_a = sorted(prices_a)[:15]
    actual_prices_a = [r[0] for r in rows_a]
    assert actual_prices_a == expected_cheapest_a

    # Для site_b записаны все 10
    actual_prices_b = [r[0] for r in rows_b]
    assert actual_prices_b == list(range(5000, 15000, 1000))


def test_cap_zero_saves_all(tmp_path):
    """Ограничение 0 пишет всё без отсечения."""
    h = PriceHistory(tmp_path / "h.sqlite", max_offers_per_site=0)

    offers_a = [Offer(site="site_a", price=1000 + i, offer_id=f"a_{i}") for i in range(100)]
    offers_b = [Offer(site="site_b", price=2000 + i, offer_id=f"b_{i}") for i in range(10)]

    res = [
        SiteResult(site="site_a", ok=True, offers=offers_a),
        SiteResult(site="site_b", ok=True, offers=offers_b),
    ]

    h.annotate(Q, res, now=1000.0)

    with h._connect() as db:
        count_a = db.execute("SELECT COUNT(*) FROM price_history WHERE site = 'site_a'").fetchone()[0]
        count_b = db.execute("SELECT COUNT(*) FROM price_history WHERE site = 'site_b'").fetchone()[0]

    assert count_a == 100
    assert count_b == 10


def test_prev_price_and_min_price_unchanged_by_cap(tmp_path):
    """Билеты, у которых offer_id уже есть в истории, получают корректный prev_price/min_price,

    даже если в текущем поиске они не входят в число самых дешёвых.
    Ограничение касается только записи новых строк в историю, но не расчёта prev/min.
    """
    db_capped = tmp_path / "capped.sqlite"
    db_uncapped = tmp_path / "uncapped.sqlite"

    h_capped = PriceHistory(db_capped, max_offers_per_site=15)
    h_uncapped = PriceHistory(db_uncapped, max_offers_per_site=0)

    # Предварительно записываем в обе базы историю для 10 билетов (по 1 билету на поиск)
    for i in range(10):
        o = Offer(site="site_a", price=10000 + i * 500, offer_id=f"tracked_{i}")
        o_copy = Offer(site="site_a", price=10000 + i * 500, offer_id=f"tracked_{i}")
        h_capped.annotate(Q, [SiteResult(site="site_a", ok=True, offers=[o])], now=1000.0 + i)
        h_uncapped.annotate(Q, [SiteResult(site="site_a", ok=True, offers=[o_copy])], now=1000.0 + i)

    # 2-й замер: 100 офферов от site_a и 10 от site_b.
    # В site_a первые 30 офферов — очень дешёвые новинки (1000..3900).
    # Наши 10 отслеживаемых билетов имеют цены 15000+ и занимают места 31..40 (не попадают в топ-15).
    offers_second_capped = []
    offers_second_uncapped = []

    # 30 дешёвых новых офферов
    for i in range(30):
        offers_second_capped.append(Offer(site="site_a", price=1000 + i * 100, offer_id=f"new_{i}"))
        offers_second_uncapped.append(Offer(site="site_a", price=1000 + i * 100, offer_id=f"new_{i}"))

    # 10 ранее известных билетов, которые подорожали
    for i in range(10):
        offers_second_capped.append(Offer(site="site_a", price=15000 + i * 500, offer_id=f"tracked_{i}"))
        offers_second_uncapped.append(Offer(site="site_a", price=15000 + i * 500, offer_id=f"tracked_{i}"))

    # 60 прочих дорогих билетов
    for i in range(60):
        offers_second_capped.append(Offer(site="site_a", price=30000 + i * 100, offer_id=f"expensive_{i}"))
        offers_second_uncapped.append(Offer(site="site_a", price=30000 + i * 100, offer_id=f"expensive_{i}"))

    # 10 билетов site_b
    offers_b_capped = [Offer(site="site_b", price=5000 + i * 100, offer_id=f"b_{i}") for i in range(10)]
    offers_b_uncapped = [Offer(site="site_b", price=5000 + i * 100, offer_id=f"b_{i}") for i in range(10)]

    res_capped = [
        SiteResult(site="site_a", ok=True, offers=offers_second_capped),
        SiteResult(site="site_b", ok=True, offers=offers_b_capped),
    ]
    res_uncapped = [
        SiteResult(site="site_a", ok=True, offers=offers_second_uncapped),
        SiteResult(site="site_b", ok=True, offers=offers_b_uncapped),
    ]

    h_capped.annotate(Q, res_capped, now=5000.0)
    h_uncapped.annotate(Q, res_uncapped, now=5000.0)

    # Проверяем: для всех отслеживаемых билетов (не вошедших в топ-15 и не записанных заново)
    # prev_price, prev_at, min_price рассчитаны точно так же, как при cap=0
    for i in range(10):
        o_cap = next(o for o in offers_second_capped if o.offer_id == f"tracked_{i}")
        o_uncap = next(o for o in offers_second_uncapped if o.offer_id == f"tracked_{i}")

        expected_prev = 10000 + i * 500
        assert o_cap.prev_price == expected_prev
        assert o_cap.min_price == expected_prev
        assert o_cap.prev_at == 1000.0 + i

        assert o_cap.prev_price == o_uncap.prev_price
        assert o_cap.min_price == o_uncap.min_price
        assert o_cap.prev_at == o_uncap.prev_at

    # При этом в БД capped записано только 15 самых дешёвых для site_a и 10 для site_b
    with h_capped._connect() as db:
        # 10 старых записей + 15 новых для site_a
        count_a = db.execute("SELECT COUNT(*) FROM price_history WHERE site = 'site_a'").fetchone()[0]
        count_b = db.execute("SELECT COUNT(*) FROM price_history WHERE site = 'site_b'").fetchone()[0]
        assert count_a == 10 + 15
        assert count_b == 10

        # Ни один из tracked_i во 2-м замере (now=5000.0) НЕ был записан в capped
        recorded_at_5000 = db.execute(
            "SELECT offer_id FROM price_history WHERE observed_at = 5000.0 AND site = 'site_a'"
        ).fetchall()
        assert len(recorded_at_5000) == 15
        recorded_ids = {r[0] for r in recorded_at_5000}
        for i in range(10):
            assert f"tracked_{i}" not in recorded_ids


def test_settings_default_and_env(monkeypatch):
    """Настройка Settings.history_max_offers_per_site по умолчанию 15 и читается из env."""
    s = Settings(sites=[])
    assert s.history_max_offers_per_site == 15

    monkeypatch.setenv("HISTORY_MAX_OFFERS_PER_SITE", "25")
    s2 = load_settings()
    assert s2.history_max_offers_per_site == 25

    monkeypatch.setenv("HISTORY_MAX_OFFERS_PER_SITE", "0")
    s3 = load_settings()
    assert s3.history_max_offers_per_site == 0


def test_price_history_env_and_property(tmp_path, monkeypatch):
    """PriceHistory считывает env HISTORY_MAX_OFFERS_PER_SITE и позволяет настраивать ограничение."""
    monkeypatch.setenv("HISTORY_MAX_OFFERS_PER_SITE", "10")
    h = PriceHistory(tmp_path / "h1.sqlite")
    assert h.history_max_offers_per_site == 10

    # Явное переопределение через свойство
    h.history_max_offers_per_site = 5
    assert h.history_max_offers_per_site == 5

    # Передача в annotate
    offers = [Offer(site="s", price=1000 + i, offer_id=f"o_{i}") for i in range(20)]
    h.annotate(Q, [SiteResult(site="s", ok=True, offers=offers)], now=100.0, max_offers_per_site=3)
    with h._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 3


def test_route_daily_rebuild_accurate(tmp_path):
    """Расчёт route_daily (min, p25, median, samples) остаётся точным."""
    h = PriceHistory(tmp_path / "h.sqlite", max_offers_per_site=15)

    target_day = date(2026, 11, 1)
    base_ts = 1793530800.0  # 2026-11-01 11:00 UTC

    # 3 поиска за один и тот же день, в каждом по 100 офферов
    for s_idx, min_p in enumerate([10000, 9500, 10500]):
        offers = [Offer(site="s", price=min_p + i * 100, offer_id=f"s{s_idx}_{i}") for i in range(100)]
        h.annotate(
            Q,
            [SiteResult(site="s", ok=True, offers=offers)],
            now=base_ts + s_idx * 600.0,
            search_id=f"search_{s_idx}",
        )

    written = h.rebuild_route_daily(target_day)
    assert written == 1

    with h._connect() as db:
        row = db.execute("SELECT min_price, p25_price, median_price, samples FROM route_daily").fetchone()

    min_price, p25_price, median_price, samples = row
    # Минимум дня — 9500
    assert min_price == 9500
    # 3 поиска -> 3 замера
    assert samples == 3
    # Медиана между 9500, 10000, 10500 = 10000
    assert median_price == 10000.0
