from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import MagicMock

import pytest

from avia import analytics
from avia.analytics import HINT_MIN_POINTS, HINT_PCT, HINT_WINDOW_DAYS, history_hint_line
from avia.bot import Bot
from avia.config import Settings, load_settings
from avia.criteria import SearchCriteria
from avia.dialog import SearchRequest
from avia.formatting import build_compact_report, offer_card
from avia.models import Offer, Query, SiteResult


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    """Изолируем MetricsStore в тестах бота, чтобы они не писали в реальный state."""
    import avia.bot
    from avia.metrics import MetricsStore

    monkeypatch.setattr(avia.bot, "MetricsStore", lambda _path: MetricsStore(tmp_path / "metrics.sqlite"))


class RecordingBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.next_msg_id = 200

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        return {"ok": True}


def test_history_hint_constants():
    assert HINT_MIN_POINTS == 10
    assert HINT_PCT == 8
    assert HINT_WINDOW_DAYS == 30


def test_history_hint_too_few_points():
    """Если замеров меньше HINT_MIN_POINTS (10), возвращается None."""
    assert history_hint_line(8000, []) is None
    assert history_hint_line(8000, [10000]) is None
    assert history_hint_line(8000, [10000] * 5) is None
    assert history_hint_line(8000, [10000] * 9) is None
    assert history_hint_line(15000, [10000] * 9) is None


def test_history_hint_thresholds():
    """Пороги: <= -8% -> 💚, >= +8% -> 🔺, внутри ±8% -> None."""
    # Медиана = 10000 по 10 точкам
    prices = [10000] * 10
    m = 10000

    # Границы: 10000 * (1 - 0.08) = 9200, 10000 * (1 + 0.08) = 10800
    # 1. Ниже или равно -8%
    assert history_hint_line(9200, prices) == (
        "💚 Ниже обычного: на 8% дешевле типичной цены маршрута (по 10 замерам)"
    )
    assert history_hint_line(9000, prices) == (
        "💚 Ниже обычного: на 10% дешевле типичной цены маршрута (по 10 замерам)"
    )
    assert history_hint_line(8000, prices) == (
        "💚 Ниже обычного: на 20% дешевле типичной цены маршрута (по 10 замерам)"
    )

    # 2. Выше или равно +8%
    assert history_hint_line(10800, prices) == (
        "🔺 Выше обычного: на 8% дороже типичной цены маршрута (по 10 замерам)"
    )
    assert history_hint_line(11000, prices) == (
        "🔺 Выше обычного: на 10% дороже типичной цены маршрута (по 10 замерам)"
    )
    assert history_hint_line(12500, prices) == (
        "🔺 Выше обычного: на 25% дороже типичной цены маршрута (по 10 замерам)"
    )

    # 3. В пределах ±8% -> None (обычная цена не комментируется)
    assert history_hint_line(9201, prices) is None
    assert history_hint_line(9500, prices) is None
    assert history_hint_line(10000, prices) is None
    assert history_hint_line(10500, prices) is None
    assert history_hint_line(10799, prices) is None


def test_history_hint_percentage_rounding_and_counts():
    """Округление процента и число замеров N."""
    prices = [10000] * 15
    # diff = 1260 -> 12.6% -> round -> 13%
    assert history_hint_line(8740, prices) == (
        "💚 Ниже обычного: на 13% дешевле типичной цены маршрута (по 15 замерам)"
    )
    # diff = 1240 -> 12.4% -> round -> 12%
    assert history_hint_line(8760, prices) == (
        "💚 Ниже обычного: на 12% дешевле типичной цены маршрута (по 15 замерам)"
    )
    # diff = 1260 -> 12.6% -> round -> 13% дороже
    assert history_hint_line(11260, prices) == (
        "🔺 Выше обычного: на 13% дороже типичной цены маршрута (по 15 замерам)"
    )
    # diff = 1240 -> 12.4% -> round -> 12% дороже
    assert history_hint_line(11240, prices) == (
        "🔺 Выше обычного: на 12% дороже типичной цены маршрута (по 15 замерам)"
    )


def test_history_hint_invalid_values():
    """Некорректные значения best_price или цен маршрута возвращают None без исключений."""
    assert history_hint_line(0, [10000] * 10) is None
    assert history_hint_line(-500, [10000] * 10) is None
    assert history_hint_line(5000, [0] * 10) is None
    assert history_hint_line(5000, None) is None  # type: ignore[arg-type]
    assert history_hint_line(5000, "invalid") is None  # type: ignore[arg-type]


def test_history_hint_config_flag(monkeypatch):
    """Флаг HISTORY_HINT в настройках: по умолчанию True, при '0' False."""
    monkeypatch.delenv("HISTORY_HINT", raising=False)
    settings = load_settings()
    assert settings.history_hint is True

    monkeypatch.setenv("HISTORY_HINT", "0")
    settings0 = load_settings()
    assert settings0.history_hint is False

    monkeypatch.setenv("HISTORY_HINT", "1")
    settings1 = load_settings()
    assert settings1.history_hint is True


def test_offer_card_and_compact_report_formatting():
    """Строка подсказки вставляется рядом с лучшей ценой в первой карточке."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    o1 = Offer(site="site_a", price=8000, airline="Аэрофлот")
    o2 = Offer(site="site_b", price=12000, airline="S7 Airlines")

    # 1. Проверяем offer_card
    hint = "💚 Ниже обычного: на 20% дешевле типичной цены маршрута (по 10 замерам)"
    card_lines = offer_card(o1, q, number=1, title="Самый дешёвый", history_hint=hint)
    card_text = "\n".join(card_lines)
    assert hint in card_text
    assert "<b>8 000 ₽</b>  ·  Аэрофлот\n💚 Ниже обычного:" in card_text

    # 2. Проверяем build_compact_report
    results = [SiteResult("site_a", True, [o1, o2])]
    chunks, flat, pool = build_compact_report(
        q, results, SearchCriteria(), history_hint=hint
    )
    full_text = "\n".join(chunks)
    assert hint in full_text
    # Подсказка присутствует только 1 раз (у лучшей цены, не дублируется у o2)
    assert full_text.count(hint) == 1

    # Без подсказки (history_hint=None)
    chunks_no_hint, _, _ = build_compact_report(
        q, results, SearchCriteria(), history_hint=None
    )
    assert "Ниже обычного" not in "\n".join(chunks_no_hint)
    assert "Выше обычного" not in "\n".join(chunks_no_hint)


@pytest.mark.asyncio
async def test_bot_live_search_with_history_hint(monkeypatch, tmp_path):
    """Живой поиск в боте формирует строку 'ниже/выше обычного' при наличии истории."""
    settings = Settings(
        sites=[],
        tg_token="token_test",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
        history_hint=True,
    )
    bot = RecordingBot(settings)
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))

    # Мокаем search: возвращает билет за 8000 ₽
    async def mock_search(query, s, priority=None):
        return [SiteResult("aviasales", True, [Offer("aviasales", 8000, airline="Победа")])]

    # Мокаем route_search_minima в bot.history: 10 замеров по 10000 ₽
    bot.history = MagicMock()
    bot.history.annotate = MagicMock()
    bot.history.route_search_minima = MagicMock(return_value=[10000] * 10)

    monkeypatch.setattr("avia.bot.search", mock_search)

    req = SearchRequest(query=q, baggage_mode="any", criteria=SearchCriteria())
    await bot._run_search(123, req)

    # Проверяем отправленные сообщения
    send_calls = [params for method, params in bot.calls if method == "sendMessage"]
    assert len(send_calls) >= 1
    # Последнее отправленное сообщение — карточка результатов
    final_card = send_calls[-1]["text"]
    expected_hint = "💚 Ниже обычного: на 20% дешевле типичной цены маршрута (по 10 замерам)"
    assert expected_hint in final_card
    bot.history.route_search_minima.assert_called_once()


@pytest.mark.asyncio
async def test_bot_live_search_flag_disabled(monkeypatch, tmp_path):
    """При Settings.history_hint=False строка не добавляется даже при наличии истории."""
    settings = Settings(
        sites=[],
        tg_token="token_test",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
        history_hint=False,
    )
    bot = RecordingBot(settings)
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))

    async def mock_search(query, s, priority=None):
        return [SiteResult("aviasales", True, [Offer("aviasales", 8000, airline="Победа")])]

    bot.history = MagicMock()
    bot.history.annotate = MagicMock()
    bot.history.route_search_minima = MagicMock(return_value=[10000] * 10)

    monkeypatch.setattr("avia.bot.search", mock_search)

    req = SearchRequest(query=q, baggage_mode="any", criteria=SearchCriteria())
    await bot._run_search(123, req)

    send_calls = [params for method, params in bot.calls if method == "sendMessage"]
    final_card = send_calls[-1]["text"]
    assert "Ниже обычного" not in final_card
    assert "Выше обычного" not in final_card
    bot.history.route_search_minima.assert_not_called()


@pytest.mark.asyncio
async def test_bot_live_search_history_error_fallback(monkeypatch, tmp_path):
    """Ошибка в route_search_minima логируется и не ломает выдачу (выдача без строки)."""
    settings = Settings(
        sites=[],
        tg_token="token_test",
        tg_allowed_ids={123},
        storage_dir=tmp_path,
        history_hint=True,
    )
    bot = RecordingBot(settings)
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))

    async def mock_search(query, s, priority=None):
        return [SiteResult("aviasales", True, [Offer("aviasales", 8000, airline="Победа")])]

    bot.history = MagicMock()
    bot.history.annotate = MagicMock()
    # Имитируем сбой БД SQLite в истории
    bot.history.route_search_minima = MagicMock(side_effect=RuntimeError("SQLite database locked"))

    monkeypatch.setattr("avia.bot.search", mock_search)

    req = SearchRequest(query=q, baggage_mode="any", criteria=SearchCriteria())
    # Не должно бросать исключение
    await bot._run_search(123, req)

    send_calls = [params for method, params in bot.calls if method == "sendMessage"]
    final_card = send_calls[-1]["text"]
    assert "8 000" in final_card
    assert "Ниже обычного" not in final_card
    assert "Выше обычного" not in final_card
