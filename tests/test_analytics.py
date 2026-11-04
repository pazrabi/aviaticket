"""Тесты модуля аналитики цен (analytics.py)."""
import pytest

from avia.analytics import (
    best_site_line,
    position_line,
    site_label,
    summary_lines,
    trend_line,
    volatility_line,
)


class TestPositionLine:
    def test_none_or_missing(self):
        assert position_line(None) is None
        assert position_line({}) is None
        assert position_line({"percentile_of_last": None}) is None

    def test_cheaper_than_usual_boundaries(self):
        # Граничное значение 80
        assert position_line({"percentile_of_last": 80.0}) == "💚 Дешевле, чем в 80% замеров по этому маршруту"
        assert position_line({"percentile_of_last": 80}) == "💚 Дешевле, чем в 80% замеров по этому маршруту"
        assert position_line({"percentile_of_last": 95.2}) == "💚 Дешевле, чем в 95% замеров по этому маршруту"
        assert position_line({"percentile_of_last": 100.0}) == "💚 Дешевле, чем в 100% замеров по этому маршруту"

    def test_normal_price_boundaries(self):
        # 40–79%
        assert position_line({"percentile_of_last": 79.4}) == "Цена обычная: дешевле, чем в 79% замеров"
        assert position_line({"percentile_of_last": 50.0}) == "Цена обычная: дешевле, чем в 50% замеров"
        # Граничное значение 40
        assert position_line({"percentile_of_last": 40.0}) == "Цена обычная: дешевле, чем в 40% замеров"
        assert position_line({"percentile_of_last": 40}) == "Цена обычная: дешевле, чем в 40% замеров"

    def test_expensive_than_usual_boundaries(self):
        # < 40% (M = 100 - N)
        assert position_line({"percentile_of_last": 39.0}) == "🔺 Дороже, чем в 61% замеров"
        assert position_line({"percentile_of_last": 20.0}) == "🔺 Дороже, чем в 80% замеров"
        assert position_line({"percentile_of_last": 0.0}) == "🔺 Дороже, чем в 100% замеров"


class TestTrendLine:
    def test_none_or_both_none(self):
        assert trend_line(None) is None
        assert trend_line({}) is None
        assert trend_line({"trend_3d": None, "trend_7d": None}) is None

    def test_identical_trends_glued(self):
        assert trend_line({"trend_3d": "up", "trend_7d": "up"}) == "📈 За 3 и 7 дней растёт"
        assert trend_line({"trend_3d": "down", "trend_7d": "down"}) == "📉 За 3 и 7 дней падает"
        assert trend_line({"trend_3d": "flat", "trend_7d": "flat"}) == "➡️ За 3 и 7 дней стоит"

    def test_different_trends(self):
        assert trend_line({"trend_3d": "up", "trend_7d": "down"}) == "📈 За 3 дня растёт, за 7 дней падает"
        assert trend_line({"trend_3d": "down", "trend_7d": "flat"}) == "📉 За 3 дня падает, за 7 дней стоит"
        assert trend_line({"trend_3d": "flat", "trend_7d": "up"}) == "➡️ За 3 дня стоит, за 7 дней растёт"

    def test_single_trend(self):
        assert trend_line({"trend_3d": "up", "trend_7d": None}) == "📈 За 3 дня растёт"
        assert trend_line({"trend_3d": None, "trend_7d": "down"}) == "📉 За 7 дней падает"


class TestVolatilityLine:
    def test_none_or_missing(self):
        assert volatility_line(None) is None
        assert volatility_line({}) is None
        assert volatility_line({"volatility_pct": None}) is None

    def test_stable_boundaries(self):
        # < 5%
        assert volatility_line({"volatility_pct": 0.0}) == "Цена стабильная"
        assert volatility_line({"volatility_pct": 3.2}) == "Цена стабильная"
        assert volatility_line({"volatility_pct": 4.9}) == "Цена стабильная"

    def test_volatile_boundaries(self):
        # Граничное значение 5%
        assert volatility_line({"volatility_pct": 5.0}) == "Цена скачет ±5%"
        assert volatility_line({"volatility_pct": 12.3}) == "Цена скачет ±12%"
        assert volatility_line({"volatility_pct": 15.8}) == "Цена скачет ±16%"


class TestBestSiteLine:
    def test_none_or_empty(self):
        assert best_site_line(None) is None
        assert best_site_line([]) is None

    def test_less_than_five_samples(self):
        # Меньше 5 замеров -> None
        assert best_site_line([("aviasales", 3, 4)]) is None
        assert best_site_line([("aviasales", 1, 1)]) is None

    def test_no_wins(self):
        assert best_site_line([("aviasales", 0, 10)]) is None

    def test_site_names_and_escaping(self):
        # Проверяем маппинг популярных названий
        assert best_site_line([("aviasales", 6, 8)]) == "Чаще всего дешевле на Aviasales (в 6 из 8 замеров)"
        assert best_site_line([("kupibilet", 5, 5)]) == "Чаще всего дешевле на Купибилет (в 5 из 5 замеров)"
        assert best_site_line([("yandex_travel", 4, 6)]) == "Чаще всего дешевле на Яндекс Путешествия (в 4 из 6 замеров)"
        assert best_site_line([("tutu", 5, 7)]) == "Чаще всего дешевле на Туту (в 5 из 7 замеров)"
        assert best_site_line([("onetwotrip", 7, 10)]) == "Чаще всего дешевле на OneTwoTrip (в 7 из 10 замеров)"

        # Проверяем экранирование спецсимволов HTML
        assert best_site_line([("site<test>&co", 5, 8)]) == "Чаще всего дешевле на site&lt;test&gt;&amp;co (в 5 из 8 замеров)"


class TestSummaryLines:
    def test_order_and_filtering(self):
        stats = {
            "percentile_of_last": 85.0,
            "trend_3d": "up",
            "trend_7d": "up",
            "volatility_pct": 2.1,
        }
        site_stats = [("aviasales", 6, 8), ("kupibilet", 2, 8)]
        lines = summary_lines(stats, site_stats)
        assert lines == [
            "💚 Дешевле, чем в 85% замеров по этому маршруту",
            "📈 За 3 и 7 дней растёт",
            "Цена стабильная",
            "Чаще всего дешевле на Aviasales (в 6 из 8 замеров)",
        ]

    def test_empty_when_no_data(self):
        assert summary_lines(None, None) == []
        assert summary_lines({}, []) == []

    def test_partial_data(self):
        stats = {"percentile_of_last": 30.0}
        lines = summary_lines(stats, None)
        assert lines == ["🔺 Дороже, чем в 70% замеров"]
