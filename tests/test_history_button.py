"""Тесты кнопки «📊 История», построения графика, fallback-вариантов и rate limit."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from avia.bot import Bot
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.dialog import Dialog, HistoryRequest, Reply, Session, Step, compact_keyboard
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult
from avia.store import ResultStore
from avia.subscriptions import SubscriptionStore


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
        if method == "answerCallbackQuery":
            return True
        if method == "editMessageText":
            return True
        return {"ok": True}

    async def send_photo(self, chat_id: int, png_bytes: bytes, caption: str | None = None):
        self.photos.append({"chat_id": chat_id, "png": png_bytes, "caption": caption})
        return {"message_id": 999}


def make_bot(tmp_path, allowed_ids: set[int] | None = None) -> FakeBot:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="secret_token_123",
        tg_allowed_ids=allowed_ids or {1},
        storage_dir=storage,
    )
    return FakeBot(settings)


def test_compact_keyboard_contains_history_button():
    """compact_keyboard содержит кнопку '📊 История' отдельной строкой перед строкой нового поиска."""
    d_id = "d_abc"
    kb = compact_keyboard(d_id, has_more=True)

    # Строка истории
    hist_rows = [row for row in kb if any(b[1] == f"hist:{d_id}" for b in row)]
    assert len(hist_rows) == 1
    assert hist_rows[0] == [("📊 История", f"hist:{d_id}")]

    # Находится перед строкой с новым поиском
    hist_idx = kb.index(hist_rows[0])
    new_rows = [i for i, row in enumerate(kb) if any(b[1].startswith("new:") for b in row)]
    assert len(new_rows) == 1
    assert hist_idx < new_rows[0]


def test_on_callback_hist_dialog_id(tmp_path):
    """hist:<dialog_id> извлекает Query из store или session и возвращает HistoryRequest."""
    store = ResultStore()
    subs = SubscriptionStore(tmp_path / "subs.sqlite")
    dialog = Dialog(store=store, subs=subs)
    chat_id = 10

    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1), return_date=date(2026, 12, 5))
    store.put(chat_id, q, [], "any", [])

    # Из store
    res = dialog.on_callback(chat_id, "hist:dialog123")
    assert len(res) == 1
    assert isinstance(res[0], HistoryRequest)
    assert res[0].query.origin == "KUF"
    assert res[0].query.destination == "EVN"

    # Из session, если store пуст
    store._data.clear()
    dialog.sessions[chat_id] = Session(
        step=Step.DONE,
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 10),
        dialog_id="dialog_sess",
    )
    res_sess = dialog.on_callback(chat_id, "hist:dialog_sess")
    assert len(res_sess) == 1
    assert isinstance(res_sess[0], HistoryRequest)
    assert res_sess[0].query.origin == "MOW"
    assert res_sess[0].query.destination == "LED"

    # Если данных нет вообще -> Reply с фразой «Пока мало данных…»
    dialog.sessions.clear()
    res_empty = dialog.on_callback(chat_id, "hist:unknown")
    assert len(res_empty) == 1
    assert isinstance(res_empty[0], Reply)
    assert "Пока мало данных" in res_empty[0].text


def test_on_callback_hist_sub(tmp_path):
    """hist:sub:<sub_id>: своя подписка возвращает HistoryRequest, чужая или несуществующая — отказ."""
    subs = SubscriptionStore(tmp_path / "subs.sqlite")
    dialog = Dialog(subs=subs)
    q1 = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))
    sub_user1 = subs.create(100, q1, "any", SearchCriteria())

    # Своя подписка
    res_own = dialog.on_callback(100, f"hist:sub:{sub_user1.id}")
    assert len(res_own) == 1
    assert isinstance(res_own[0], HistoryRequest)
    assert res_own[0].query.origin == "KUF"

    # Чужая подписка недоступна
    res_alien = dialog.on_callback(200, f"hist:sub:{sub_user1.id}")
    assert len(res_alien) == 1
    assert isinstance(res_alien[0], Reply)
    assert "Пока мало данных" in res_alien[0].text

    # Несуществующая подписка
    res_nonexistent = dialog.on_callback(100, "hist:sub:99999")
    assert len(res_nonexistent) == 1
    assert isinstance(res_nonexistent[0], Reply)
    assert "Пока мало данных" in res_nonexistent[0].text


@pytest.mark.asyncio
async def test_bot_process_history_request_with_png(tmp_path, monkeypatch):
    """HistoryRequest при наличии PNG отправляет фото с заголовком и аналитикой."""
    bot = make_bot(tmp_path)
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))

    # Заполняем историю
    for day in range(5):
        bot.history.annotate(
            q,
            [SiteResult("aviasales", True, [Offer("aviasales", price=10000 - day * 200, offer_id=f"o{day}")])],
            now=1000000.0 + day * 86400,
            search_id=f"s{day}",
        )

    # Подменяем charts.render_price_chart
    monkeypatch.setattr("avia.charts.render_price_chart", lambda series, median, title, now_price: b"fake_png_data")

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 1
    photo = bot.photos[0]
    assert photo["chat_id"] == 100
    assert photo["png"] == b"fake_png_data"
    assert "📊 <b>История цены</b>" in photo["caption"]


@pytest.mark.asyncio
async def test_bot_process_history_request_fallback_text(tmp_path, monkeypatch):
    """HistoryRequest при png=None, но наличии stats отправляет текст."""
    bot = make_bot(tmp_path)
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))

    # Заполняем историю
    for day in range(4):
        bot.history.annotate(
            q,
            [SiteResult("aviasales", True, [Offer("aviasales", price=10000, offer_id=f"o{day}")])],
            now=1000000.0 + day * 86400,
            search_id=f"s{day}",
        )

    monkeypatch.setattr("avia.charts.render_price_chart", lambda series, median, title, now_price: None)

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 0
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "📊 <b>История цены</b>" in send_calls[0]["text"]


@pytest.mark.asyncio
async def test_bot_process_history_request_no_data(tmp_path):
    """HistoryRequest при отсутствии данных отправляет 'Пока мало данных…'."""
    bot = make_bot(tmp_path)
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 0
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "Пока мало данных" in send_calls[0]["text"]


@pytest.mark.asyncio
async def test_bot_process_history_request_error_handling(tmp_path, monkeypatch):
    """При неожиданном исключении во время построения графика отправляется 'Не удалось построить график'."""
    bot = make_bot(tmp_path)
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))

    def _broken_stats(q):
        raise RuntimeError("database crashed")

    monkeypatch.setattr(bot.history, "route_stats", _broken_stats)

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 0
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "Не удалось построить график" in send_calls[0]["text"]


@pytest.mark.asyncio
async def test_history_rate_limit(tmp_path, monkeypatch):
    """Повторное нажатие hist в течение 5 секунд даёт answerCallbackQuery('Секунду…')."""
    bot = make_bot(tmp_path)
    chat_id = 1
    d_id = "test_rate"
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    bot.store.put(chat_id, q, [], "any", [])

    monkeypatch.setattr("avia.charts.render_price_chart", lambda *args, **kwargs: None)

    cq1 = {
        "id": "cq_1",
        "from": {"id": chat_id},
        "message": {"chat": {"id": chat_id}, "message_id": 10},
        "data": f"hist:{d_id}",
    }

    # Первое нажатие
    await bot.handle_callback(cq1)
    ans1 = [p for m, p in bot.calls if m == "answerCallbackQuery" and p.get("callback_query_id") == "cq_1"]
    assert len(ans1) == 1
    assert ans1[0].get("text") is None  # обычный ответ

    # Второе нажатие сразу же
    cq2 = {
        "id": "cq_2",
        "from": {"id": chat_id},
        "message": {"chat": {"id": chat_id}, "message_id": 10},
        "data": f"hist:{d_id}",
    }
    await bot.handle_callback(cq2)
    ans2 = [p for m, p in bot.calls if m == "answerCallbackQuery" and p.get("callback_query_id") == "cq_2"]
    assert len(ans2) == 1
    assert ans2[0].get("text") == "Секунду…"

    # Другой чат не заблокирован
    cq_other = {
        "id": "cq_3",
        "from": {"id": 2},
        "message": {"chat": {"id": 2}, "message_id": 10},
        "data": f"hist:{d_id}",
    }
    bot.s.tg_open = True
    await bot.handle_callback(cq_other)
    ans3 = [p for m, p in bot.calls if m == "answerCallbackQuery" and p.get("callback_query_id") == "cq_3"]
    assert len(ans3) == 1
    assert ans3[0].get("text") is None

    # По истечении 5 секунд первый чат снова работает
    bot._hist_times[chat_id] -= 6.0
    cq4 = {
        "id": "cq_4",
        "from": {"id": chat_id},
        "message": {"chat": {"id": chat_id}, "message_id": 10},
        "data": f"hist:{d_id}",
    }
    await bot.handle_callback(cq4)
    ans4 = [p for m, p in bot.calls if m == "answerCallbackQuery" and p.get("callback_query_id") == "cq_4"]
    assert len(ans4) == 1
    assert ans4[0].get("text") is None


@pytest.mark.asyncio
async def test_send_photo_method(tmp_path, monkeypatch, caplog):
    """Метод Bot.send_photo выполняет multipart POST, обрезает caption и не логирует токен."""
    # Используем настоящий Bot без переопределения send_photo
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        sites=[],
        tg_token="my_secret_token_xyz",
        tg_allowed_ids={1},
        storage_dir=storage,
    )
    bot = Bot(settings)

    captured_req: dict = {}

    async def fake_post(url, data=None, files=None):
        captured_req["url"] = url
        captured_req["data"] = data
        captured_req["files"] = files
        class FakeResponse:
            def json(self):
                return {"ok": True, "result": {"message_id": 555}}
        return FakeResponse()

    monkeypatch.setattr(bot.http, "post", fake_post)

    long_caption = "A" * 1500
    res = await bot.send_photo(777, b"fake_bytes", caption=long_caption)
    assert res == {"message_id": 555}
    assert "sendPhoto" in captured_req["url"]
    assert captured_req["data"]["chat_id"] == "777"
    assert len(captured_req["data"]["caption"]) == 1024
    assert captured_req["data"]["parse_mode"] == "HTML"
    assert "photo" in captured_req["files"]
    assert captured_req["files"]["photo"][1] == b"fake_bytes"

    # Проверка, что токен не логируется при ошибке
    async def failing_post(url, data=None, files=None):
        raise RuntimeError("network failure")

    monkeypatch.setattr(bot.http, "post", failing_post)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            await bot.send_photo(777, b"fake_bytes", caption="error test")
        assert "my_secret_token_xyz" not in caplog.text


@pytest.mark.asyncio
async def test_bot_history_request_advice_inclusion_and_resilience(tmp_path, monkeypatch):
    """HistoryRequest содержит блок совета при наличии достаточного числа замеров, а ошибки price_curve игнорируются."""
    bot = make_bot(tmp_path)
    q = Query(origin="KUF", destination="EVN", depart=date.today() + date.resolution * 12)

    # 12 замеров
    for day in range(12):
        bot.history.annotate(
            q,
            [SiteResult("aviasales", True, [Offer("aviasales", price=10000, offer_id=f"o{day}")])],
            now=1000000.0 + day * 86400,
            search_id=f"s{day}",
        )

    # Имитируем сбой в price_curve — не должен ломать построение истории
    monkeypatch.setattr(bot.history, "price_curve", MagicMock(side_effect=RuntimeError("curve failed")))
    monkeypatch.setattr("avia.charts.render_price_chart", lambda series, median, title, now_price: b"fake_png")

    await bot._process_actions(100, [HistoryRequest(query=q)])

    assert len(bot.photos) == 1
    photo = bot.photos[0]
    assert "📊 <b>История цены</b>" in photo["caption"]
    assert "💡 <b>Совет: брать</b>" in photo["caption"]

