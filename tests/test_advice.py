"""Тесты модуля советов «брать или ждать» (advice.py), кривой цен и их интеграции."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import html
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from avia import advice
from avia.advice import (
    Advice,
    DISCLAIMER_HTML,
    VERDICT_TITLES,
    build_advice,
    confidence_level,
    format_advice,
)
from avia.bot import Bot
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.dialog import HistoryRequest
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult
from avia.scheduler import Scheduler, build_notification
from avia.subscriptions import Subscription, SubscriptionStore


# =====================================================================
# 1. Тесты уровня уверенности (confidence_level) на границах 10 / 30 / 100
# =====================================================================


class TestConfidenceLevel:
    def test_low_confidence_boundaries(self):
        # < 10 и 10–29 дают "низкая"
        assert confidence_level(0) == "низкая"
        assert confidence_level(5) == "низкая"
        assert confidence_level(9) == "низкая"
        assert confidence_level(10) == "низкая"
        assert confidence_level(29) == "низкая"

    def test_medium_confidence_boundaries(self):
        # 30–99 дают "средняя"
        assert confidence_level(30) == "средняя"
        assert confidence_level(50) == "средняя"
        assert confidence_level(99) == "средняя"

    def test_high_confidence_boundaries(self):
        # 100+ дают "высокая"
        assert confidence_level(100) == "высокая"
        assert confidence_level(101) == "высокая"
        assert confidence_level(500) == "высокая"


# =====================================================================
# 2. Тесты build_advice: возврат None при отсутствии данных или малом числе замеров
# =====================================================================


class TestBuildAdviceNone:
    def test_stats_none_or_empty(self):
        assert build_advice(None, days_to_departure=10) is None
        assert build_advice({}, days_to_departure=10) is None

    def test_samples_less_than_ten(self):
        # При samples < 10 совет вообще не даём (None)
        assert build_advice({"samples": 9, "percentile_of_last": 50.0}, days_to_departure=10) is None
        assert build_advice({"samples": 0, "percentile_of_last": 85.0}, days_to_departure=5) is None
        assert build_advice({"samples": None, "percentile_of_last": 85.0}, days_to_departure=5) is None

    def test_percentile_none(self):
        assert build_advice({"samples": 20, "percentile_of_last": None}, days_to_departure=10) is None

    def test_days_none_or_negative(self):
        stats = {"samples": 20, "percentile_of_last": 50.0}
        assert build_advice(stats, days_to_departure=None) is None
        assert build_advice(stats, days_to_departure=-1) is None
        assert build_advice(stats, days_to_departure=-10) is None


# =====================================================================
# 3. Базовые правила вердикта на границах: d=14/15/45/46, p=30/80/40
# =====================================================================


class TestBuildAdviceBoundaries:
    # --- Ветка d <= 14 ---
    def test_d_le_14_p_ge_40(self):
        # Граница d=14, p=40
        stats = {"samples": 20, "percentile_of_last": 40.0}
        adv = build_advice(stats, days_to_departure=14)
        assert adv is not None
        assert adv.verdict == "buy"
        assert adv.text == "Лучше брать: до вылета 14 дн., ближе к вылету цены обычно растут"
        assert adv.confidence == "низкая"

        # d=0 (вылет сегодня), p=50
        adv0 = build_advice({"samples": 30, "percentile_of_last": 50.0}, days_to_departure=0)
        assert adv0 is not None
        assert adv0.verdict == "buy"
        assert adv0.text == "Лучше брать: до вылета 0 дн., ближе к вылету цены обычно растут"
        assert adv0.confidence == "средняя"

    def test_d_le_14_p_lt_40(self):
        # Граница p < 40 (цена выше обычной)
        stats = {"samples": 50, "percentile_of_last": 39.9}
        adv = build_advice(stats, days_to_departure=14)
        assert adv is not None
        assert adv.verdict == "buy"
        assert adv.text == "Цена выше обычной, но до вылета 14 дн.: ждать рискованно, цены обычно растут"
        assert adv.confidence == "средняя"

        # d=5, p=10
        adv5 = build_advice({"samples": 100, "percentile_of_last": 10.0}, days_to_departure=5)
        assert adv5 is not None
        assert adv5.verdict == "buy"
        assert adv5.text == "Цена выше обычной, но до вылета 5 дн.: ждать рискованно, цены обычно растут"
        assert adv5.confidence == "высокая"

    # --- Ветка 15 <= d <= 45 ---
    def test_15_to_45_buy_boundary(self):
        # d=15, p=80
        adv15 = build_advice({"samples": 15, "percentile_of_last": 80.0}, days_to_departure=15)
        assert adv15 is not None
        assert adv15.verdict == "buy"
        assert adv15.text == "Цена в нижних 20% за всё время наблюдений, до вылета 15 дн. — лучше брать"

        # d=45, p=80
        adv45 = build_advice({"samples": 15, "percentile_of_last": 80.0}, days_to_departure=45)
        assert adv45 is not None
        assert adv45.verdict == "buy"
        assert adv45.text == "Цена в нижних 20% за всё время наблюдений, до вылета 45 дн. — лучше брать"

    def test_15_to_45_wait_boundary(self):
        # d=15, p=30
        adv15 = build_advice({"samples": 15, "percentile_of_last": 30.0}, days_to_departure=15)
        assert adv15 is not None
        assert adv15.verdict == "wait"
        assert adv15.text == "Цена выше обычной, до вылета 15 дн. — можно подождать и последить"

        # d=45, p=30
        adv45 = build_advice({"samples": 15, "percentile_of_last": 30.0}, days_to_departure=45)
        assert adv45 is not None
        assert adv45.verdict == "wait"
        assert adv45.text == "Цена выше обычной, до вылета 45 дн. — можно подождать и последить"

    def test_15_to_45_neutral_boundary(self):
        # Чуть ниже 80 (79.9) и чуть выше 30 (30.1)
        adv_high = build_advice({"samples": 15, "percentile_of_last": 79.9}, days_to_departure=20)
        assert adv_high is not None
        assert adv_high.verdict == "neutral"
        assert adv_high.text == "Цена обычная, до вылета 20 дн. — можно брать или подождать; включите отслеживание"

        adv_low = build_advice({"samples": 15, "percentile_of_last": 30.1}, days_to_departure=20)
        assert adv_low is not None
        assert adv_low.verdict == "neutral"
        assert adv_low.text == "Цена обычная, до вылета 20 дн. — можно брать или подождать; включите отслеживание"

    # --- Ветка d > 45 ---
    def test_d_gt_45_boundaries(self):
        # Граница d=46, p=80 -> buy
        adv46_buy = build_advice({"samples": 20, "percentile_of_last": 80.0}, days_to_departure=46)
        assert adv46_buy is not None
        assert adv46_buy.verdict == "buy"
        assert adv46_buy.text == "Цена в нижних 20%, хотя вылет не скоро (46 дн.) — хорошая возможность"

        # d=46, p=79.9 -> wait
        adv46_wait = build_advice({"samples": 20, "percentile_of_last": 79.9}, days_to_departure=46)
        assert adv46_wait is not None
        assert adv46_wait.verdict == "wait"
        assert adv46_wait.text == "До вылета 46 дн.: торопиться незачем, можно подождать"

        # d=100, p=10 -> wait
        adv100 = build_advice({"samples": 100, "percentile_of_last": 10.0}, days_to_departure=100)
        assert adv100 is not None
        assert adv100.verdict == "wait"
        assert adv100.text == "До вылета 100 дн.: торопиться незачем, можно подождать"


# =====================================================================
# 4. Тесты оговорок по тренду за 7 дней
# =====================================================================


class TestTrendAdjustments:
    def test_trend_up_d_le_30_wait_to_neutral(self):
        # d <= 30, verdict 'wait', trend_7d == 'up' -> verdict 'neutral' и оговорка
        stats = {"samples": 25, "percentile_of_last": 25.0, "trend_7d": "up"}
        adv = build_advice(stats, days_to_departure=30)
        assert adv is not None
        assert adv.verdict == "neutral"
        assert "цены за неделю растут" in adv.text
        assert adv.text.endswith("цены за неделю растут")

        # d=15, p=20, trend_7d='up'
        adv15 = build_advice(stats, days_to_departure=15)
        assert adv15 is not None
        assert adv15.verdict == "neutral"
        assert "цены за неделю растут" in adv15.text

    def test_trend_up_d_gt_30_does_not_change(self):
        # d=31 (> 30) -> verdict остаётся 'wait', оговорка не добавляется
        stats = {"samples": 25, "percentile_of_last": 25.0, "trend_7d": "up"}
        adv = build_advice(stats, days_to_departure=31)
        assert adv is not None
        assert adv.verdict == "wait"
        assert "цены за неделю растут" not in adv.text

    def test_trend_down_d_gt_14_buy_adds_caveat(self):
        # verdict 'buy', trend_7d == 'down', d > 14 -> verdict остаётся 'buy', добавляется оговорка
        stats = {"samples": 35, "percentile_of_last": 90.0, "trend_7d": "down"}
        adv = build_advice(stats, days_to_departure=15)
        assert adv is not None
        assert adv.verdict == "buy"
        assert "цены за неделю падают, можно ещё подождать" in adv.text

        # d=50, p=85
        adv50 = build_advice(stats, days_to_departure=50)
        assert adv50 is not None
        assert adv50.verdict == "buy"
        assert "цены за неделю падают, можно ещё подождать" in adv50.text

    def test_trend_down_d_le_14_buy_no_caveat(self):
        # d=14 (<= 14) -> оговорка не добавляется, так как вылет слишком близко
        stats = {"samples": 35, "percentile_of_last": 90.0, "trend_7d": "down"}
        adv = build_advice(stats, days_to_departure=14)
        assert adv is not None
        assert adv.verdict == "buy"
        assert "цены за неделю падают" not in adv.text


# =====================================================================
# 5. Тесты фраз по кривой цен (curve)
# =====================================================================


class TestCurvePhrases:
    def test_curve_closer_higher(self):
        # d=25 попадает в "22-30"
        # ближайшая корзина ближе к вылету — "15-21"
        # ratio_ближе = 1.15, ratio_текущей = 1.0 -> отношение 1.15 >= 1.05 (+15%)
        curve = {
            "22-30": {"ratio": 1.0, "samples": 40},
            "15-21": {"ratio": 1.15, "samples": 40},
        }
        stats = {"samples": 50, "percentile_of_last": 50.0}
        adv = build_advice(stats, days_to_departure=25, curve=curve)
        assert adv is not None
        assert "по данным бота, ближе к вылету цены в среднем выше на 15%" in adv.text

    def test_curve_closer_lower(self):
        # ratio_ближе = 0.90, ratio_текущей = 1.0 -> отношение 0.90 <= 0.95 (-10%)
        curve = {
            "22-30": {"ratio": 1.0, "samples": 40},
            "15-21": {"ratio": 0.90, "samples": 40},
        }
        stats = {"samples": 50, "percentile_of_last": 50.0}
        adv = build_advice(stats, days_to_departure=25, curve=curve)
        assert adv is not None
        assert "по данным бота, ближе к вылету цены в среднем ниже на 10%" in adv.text

    def test_curve_neutral_range_no_phrase(self):
        # отношение 1.02 (между 0.95 и 1.05) -> фраза не добавляется
        curve = {
            "22-30": {"ratio": 1.0, "samples": 40},
            "15-21": {"ratio": 1.02, "samples": 40},
        }
        stats = {"samples": 50, "percentile_of_last": 50.0}
        adv = build_advice(stats, days_to_departure=25, curve=curve)
        assert adv is not None
        assert "по данным бота" not in adv.text

    def test_curve_skips_empty_baskets_to_find_closest(self):
        # "15-21" отсутствует в curve, берётся "8-14"
        curve = {
            "22-30": {"ratio": 1.0, "samples": 40},
            "8-14": {"ratio": 1.20, "samples": 50},
        }
        stats = {"samples": 50, "percentile_of_last": 50.0}
        adv = build_advice(stats, days_to_departure=25, curve=curve)
        assert adv is not None
        assert "по данным бота, ближе к вылету цены в среднем выше на 20%" in adv.text

    def test_curve_no_closer_basket(self):
        # d=5 (корзина "0-7" — самая близкая, ближе нет)
        curve = {
            "0-7": {"ratio": 1.3, "samples": 40},
            "8-14": {"ratio": 1.1, "samples": 40},
        }
        stats = {"samples": 50, "percentile_of_last": 50.0}
        adv = build_advice(stats, days_to_departure=5, curve=curve)
        assert adv is not None
        assert "по данным бота" not in adv.text

    def test_curve_current_basket_missing(self):
        # d=25 ("22-30"), но в curve нет этой корзины
        curve = {
            "15-21": {"ratio": 1.1, "samples": 40},
        }
        stats = {"samples": 50, "percentile_of_last": 50.0}
        adv = build_advice(stats, days_to_departure=25, curve=curve)
        assert adv is not None
        assert "по данным бота" not in adv.text


# =====================================================================
# 6. Тесты форматирования format_advice (эмодзи, HTML, экранирование)
# =====================================================================


class TestFormatAdvice:
    def test_format_none(self):
        assert format_advice(None) == []

    def test_format_verdict_titles(self):
        a_buy = Advice(verdict="buy", text="Текст покупки", confidence="высокая")
        lines_buy = format_advice(a_buy)
        assert len(lines_buy) == 3
        assert lines_buy[0] == "💡 <b>Совет: брать</b>"
        assert lines_buy[1] == "Текст покупки. Уверенность: высокая."
        assert lines_buy[2] == DISCLAIMER_HTML

        a_wait = Advice(verdict="wait", text="Текст ожидания", confidence="средняя")
        lines_wait = format_advice(a_wait)
        assert lines_wait[0] == "💡 <b>Совет: подождать</b>"

        a_neutral = Advice(verdict="neutral", text="Текст нейтральный", confidence="низкая")
        lines_neutral = format_advice(a_neutral)
        assert lines_neutral[0] == "💡 <b>Совет: решайте сами</b>"

    def test_html_escaping(self):
        a = Advice(
            verdict="buy",
            text="Цена < 5000 & вылет 'скоро'",
            confidence="<высокая>",
        )
        lines = format_advice(a)
        assert "&lt;" in lines[1]
        assert "&amp;" in lines[1]
        assert "<высокая>" not in lines[1]
        assert "&lt;высокая&gt;" in lines[1]


# =====================================================================
# 7. Тесты PriceHistory.price_curve на базе данных
# =====================================================================


class TestPriceCurveDatabase:
    def test_price_curve_calculation(self, tmp_path):
        db_path = tmp_path / "test_hist.sqlite"
        h = PriceHistory(db_path)
        base_time = 1700000000.0  # дата ~2023-11-14

        # Заполняем маршрут 1: MOW -> LED, вылет 2024-01-01
        # Замеры в разные дни до вылета
        q1 = Query(origin="MOW", destination="LED", depart=date(2024, 1, 1))

        # Корзина "0-7" (дней до вылета 3, цена 12000)
        h.annotate(
            q1,
            [SiteResult("s", True, [Offer("s", price=12000, offer_id="o1")])],
            now=datetime(2023, 12, 29, 12, 0).timestamp(),
            search_id="s1",
        )
        # Корзина "8-14" (дней до вылета 10, цена 10000)
        h.annotate(
            q1,
            [SiteResult("s", True, [Offer("s", price=10000, offer_id="o2")])],
            now=datetime(2023, 12, 22, 12, 0).timestamp(),
            search_id="s2",
        )
        # Корзина "15-21" (дней до вылета 18, цена 8000)
        h.annotate(
            q1,
            [SiteResult("s", True, [Offer("s", price=8000, offer_id="o3")])],
            now=datetime(2023, 12, 14, 12, 0).timestamp(),
            search_id="s3",
        )

        # Для q1: цены [12000, 10000, 8000], медиана = 10000
        # Отношения:
        # "0-7": 12000 / 10000 = 1.2
        # "8-14": 10000 / 10000 = 1.0
        # "15-21": 8000 / 10000 = 0.8

        # Маршрут 2: KUF -> EVN, вылет 2024-02-01
        q2 = Query(origin="KUF", destination="EVN", depart=date(2024, 2, 1))
        # "0-7" (вылет через 5 дн., цена 24000)
        h.annotate(
            q2,
            [SiteResult("s", True, [Offer("s", price=24000, offer_id="o4")])],
            now=datetime(2024, 1, 27, 12, 0).timestamp(),
            search_id="s4",
        )
        # "8-14" (вылет через 12 дн., цена 20000)
        h.annotate(
            q2,
            [SiteResult("s", True, [Offer("s", price=20000, offer_id="o5")])],
            now=datetime(2024, 1, 20, 12, 0).timestamp(),
            search_id="s5",
        )
        # "15-21" (вылет через 19 дн., цена 16000)
        h.annotate(
            q2,
            [SiteResult("s", True, [Offer("s", price=16000, offer_id="o6")])],
            now=datetime(2024, 1, 13, 12, 0).timestamp(),
            search_id="s6",
        )
        # Для q2: цены [24000, 20000, 16000], медиана = 20000
        # Отношения:
        # "0-7": 24000 / 20000 = 1.2
        # "8-14": 20000 / 20000 = 1.0
        # "15-21": 16000 / 20000 = 0.8

        now_test = datetime(2024, 2, 5, 12, 0).timestamp()

        # При min_samples=2 все три корзины должны быть в выдаче
        curve = h.price_curve(days=180, now=now_test, min_samples=2)
        assert "0-7" in curve
        assert "8-14" in curve
        assert "15-21" in curve
        assert curve["0-7"]["samples"] == 2
        assert curve["0-7"]["ratio"] == pytest.approx(1.2, rel=1e-3)
        assert curve["8-14"]["samples"] == 2
        assert curve["8-14"]["ratio"] == pytest.approx(1.0, rel=1e-3)
        assert curve["15-21"]["samples"] == 2
        assert curve["15-21"]["ratio"] == pytest.approx(0.8, rel=1e-3)

        # Корзины без замеров ("22-30", "31-60", "61+") не включены
        assert "22-30" not in curve
        assert "31-60" not in curve
        assert "61+" not in curve

        # При min_samples=3 ни одна корзина не должна войти
        curve_empty = h.price_curve(days=180, now=now_test, min_samples=3)
        assert curve_empty == {}

    def test_price_curve_without_days_to_departure(self, tmp_path):
        """Проверяем расчёт для записей, у которых days_to_departure в базе NULL."""
        db_path = tmp_path / "test_hist_null.sqlite"
        h = PriceHistory(db_path)
        # Вставляем напрямую с NULL в days_to_departure
        with h._connect() as db:
            db.execute(
                "INSERT INTO price_history "
                "(offer_id, site, origin, destination, depart, adults, price, currency, observed_at, days_to_departure, search_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("o1", "s", "MOW", "LED", "2026-12-10", 1, 10000, "RUB", datetime(2026, 12, 5, 12, 0).timestamp(), None, "s1"),
            )
            db.execute(
                "INSERT INTO price_history "
                "(offer_id, site, origin, destination, depart, adults, price, currency, observed_at, days_to_departure, search_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("o2", "s", "MOW", "LED", "2026-12-10", 1, 10000, "RUB", datetime(2026, 12, 1, 12, 0).timestamp(), None, "s2"),
            )
        now_ts = datetime(2026, 12, 6, 12, 0).timestamp()
        curve = h.price_curve(days=30, now=now_ts, min_samples=1)
        # Замер 1: вылет 10-го, замер 5-го -> 5 дней -> корзина "0-7"
        # Замер 2: вылет 10-го, замер 1-го -> 9 дней -> корзина "8-14"
        assert "0-7" in curve
        assert "8-14" in curve


# =====================================================================
# 8. Тесты интеграции в Bot (_handle_history_request)
# =====================================================================


class FakeBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.photos: list[dict] = []
        self.next_msg_id = 100

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        return {"ok": True}

    async def send_photo(self, chat_id: int, png_bytes: bytes, caption: str | None = None):
        self.photos.append({"chat_id": chat_id, "png": png_bytes, "caption": caption})
        return {"message_id": 999}


@pytest.mark.asyncio
async def test_bot_history_request_includes_advice(tmp_path, monkeypatch):
    """Подпись к графику истории содержит совет при наличии >= 10 замеров."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="tok123",
        tg_allowed_ids={100},
        storage_dir=storage,
    )
    bot = FakeBot(settings)
    # Вылет через 10 дней
    depart_d = date.today() + date.resolution * 10
    q = Query(origin="MOW", destination="LED", depart=depart_d)

    # Заполняем 15 замеров
    for day in range(15):
        bot.history.annotate(
            q,
            [SiteResult("aviasales", True, [Offer("aviasales", price=10000, offer_id=f"o{day}")])],
            now=1000000.0 + day * 3600,
            search_id=f"s{day}",
        )

    monkeypatch.setattr("avia.charts.render_price_chart", lambda series, median, title, now_price: b"fake_png")

    monkeypatch.setattr("avia.bot._history_is_enough", lambda s: True)

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 1
    caption = bot.photos[0]["caption"]
    assert "📊 <b>История цены</b>" in caption
    assert "💡 <b>Совет: брать</b>" in caption
    assert "<i>Это подсказка по прошлым ценам, а не обещание: цены могут вести себя иначе.</i>" in caption


@pytest.mark.asyncio
async def test_bot_history_request_truncates_disclaimer_if_too_long(tmp_path, monkeypatch):
    """Если подпись длиннее 1024 символов, оговорка-курсив выкидывается."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="tok123",
        tg_allowed_ids={100},
        storage_dir=storage,
    )
    bot = FakeBot(settings)
    depart_d = date.today() + date.resolution * 10
    q = Query(origin="MOW", destination="LED", depart=depart_d)

    for day in range(15):
        bot.history.annotate(
            q,
            [SiteResult("aviasales", True, [Offer("aviasales", price=10000, offer_id=f"o{day}")])],
            now=1000000.0 + day * 3600,
            search_id=f"s{day}",
        )

    # Имитируем длинные строки аналитики (около 950 символов)
    long_line = "Строка аналитики: " + "X" * 900
    monkeypatch.setattr("avia.analytics.summary_lines", lambda stats, site_stats: [long_line])
    monkeypatch.setattr("avia.charts.render_price_chart", lambda series, median, title, now_price: b"fake_png")
    monkeypatch.setattr("avia.bot._history_is_enough", lambda s: True)

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 1
    caption = bot.photos[0]["caption"]
    assert len(caption) <= 1024
    # Оговорка-курсив должна быть выкинута, чтобы освободить место
    assert "Это подсказка по прошлым ценам" not in caption
    assert "💡 <b>Совет: брать</b>" in caption


# =====================================================================
# 9. Тесты интеграции в Scheduler.run_once
# =====================================================================


@pytest.mark.asyncio
async def test_scheduler_run_once_includes_advice_line(tmp_path):
    """Уведомление планировщика содержит строку совета и hint остаётся последней строкой."""
    store = SubscriptionStore(tmp_path / "subs.db")
    history = PriceHistory(tmp_path / "history.db")
    clock_time = 1700000000.0
    today_date = date(2026, 12, 1)

    # Вылет через 10 дней от today_date
    depart_d = date(2026, 12, 11)
    q = Query(origin="MOW", destination="LED", depart=depart_d, adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock_time)
    store.record_price(sub.id, 10000, "RUB", "o_ref", clock_time - 3600)

    # Заполняем историю 12 замерами по 10000 ₽
    for i in range(12):
        offer = Offer(site="aviasales", price=10000, currency="RUB", offer_id=f"off_{i}", url="https://a.ru")
        history.annotate(
            q=q,
            results=[SiteResult(site="aviasales", ok=True, offers=[offer])],
            now=clock_time - (15 - i) * 3600.0,
            search_id=f"s_{i}",
        )

    # Текущий замер: цена выросла до 12000 (сработает подорожала)
    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=12000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=lambda: clock_time,
        today=lambda: today_date,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]

    # Проверяем наличие одной строки совета
    assert "💡 <b>Совет: брать</b> — " in text
    # Проверяем, что подсказка для роста остаётся строго последней строкой
    lines = text.split("\n")
    assert lines[-1] == "Если планируете лететь, возможно, стоит не откладывать покупку."


@pytest.mark.asyncio
async def test_scheduler_run_once_advice_error_does_not_break_notify(tmp_path, monkeypatch):
    """Сбой при расчёте совета не ломает отправку уведомления."""
    store = SubscriptionStore(tmp_path / "subs.db")
    history = PriceHistory(tmp_path / "history.db")
    clock_time = 1700000000.0
    today_date = date(2026, 12, 1)

    depart_d = date(2026, 12, 11)
    q = Query(origin="MOW", destination="LED", depart=depart_d, adults=1)
    sub = store.create(chat_id=101, query=q, baggage_mode="any", criteria=SearchCriteria(), now=clock_time)
    store.record_price(sub.id, 10000, "RUB", "o_ref", clock_time - 3600)

    for i in range(12):
        offer = Offer(site="aviasales", price=10000, currency="RUB", offer_id=f"off_{i}", url="https://a.ru")
        history.annotate(
            q=q,
            results=[SiteResult(site="aviasales", ok=True, offers=[offer])],
            now=clock_time - (15 - i) * 3600.0,
            search_id=f"s_{i}",
        )

    # Ломаем build_advice
    monkeypatch.setattr("avia.advice.build_advice", MagicMock(side_effect=RuntimeError("Advice failure")))

    async def search_fn(query: Query) -> list[SiteResult]:
        return [
            SiteResult(
                site="aviasales",
                ok=True,
                offers=[Offer(site="aviasales", price=12000, currency="RUB", url="https://s.ru/1")],
            )
        ]

    sent: list[tuple[int, str, dict | None]] = []

    async def notify_fn(chat_id: int, text: str, markup: dict | None) -> None:
        sent.append((chat_id, text, markup))

    sched = Scheduler(
        store=store,
        search_fn=search_fn,
        notify_fn=notify_fn,
        history=history,
        clock=lambda: clock_time,
        today=lambda: today_date,
    )

    count = await sched.run_once()
    assert count == 1
    assert len(sent) == 1
    _, text, _ = sent[0]
    # Уведомление ушло успешно, но без совета
    assert "Совет:" not in text
    assert "Если планируете лететь" in text
