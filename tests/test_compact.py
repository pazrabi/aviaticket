from __future__ import annotations

import asyncio
from datetime import date
from html import escape
import re

import pytest

from avia.bot import Bot
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.dialog import Dialog, Session, Step
from avia.formatting import build_compact_report, build_more_report
from avia.models import Leg, Offer, Query, SiteResult
from avia.store import ResultStore


def _check_html_tags_balanced(text: str) -> None:
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")
    assert len(re.findall(r"<a\b", text)) == text.count("</a>")


class FakeBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.next_msg_id = 200

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_msg_id += 1
            return {"message_id": self.next_msg_id, "chat": {"id": params.get("chat_id")}}
        if method == "answerCallbackQuery":
            return True
        if method == "deleteMessage":
            return True
        if method == "editMessageText":
            return True
        if method == "getUpdates":
            return []
        return {"ok": True}


def make_fake_bot(allowed_ids: set[int] | None = None) -> FakeBot:
    settings = Settings(
        sites=[],
        tg_token="secret_test_token_12345",
        tg_allowed_ids=allowed_ids or {123},
    )
    return FakeBot(settings)


def test_compact_report_default_criteria():
    """Компактный ответ при дефолтных критериях: 1-3 карточки, нумерация, flat, ссылка Озона, ошибка сайта."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o1 = Offer(
        site="site_a",
        price=10000,
        airline="A1",
        depart_time="10:00",
        duration="5 ч",
        url="https://site_a.ru/1",
        url_is_direct=False,
    )
    o2 = Offer(
        site="site_b",
        price=15000,
        airline="B1",
        depart_time="12:00",
        duration="3 ч",
        url="https://site_b.ru/direct",
        url_is_direct=True,
        layovers=["Стамбул"],
    )
    results = [
        SiteResult("site_a", True, [o1]),
        SiteResult("site_b", True, [o2]),
        SiteResult("bad_site", False, [], error="таймаут"),
    ]

    criteria = SearchCriteria()
    chunks, flat, pool = build_compact_report(q, results, criteria)

    assert len(chunks) == 1
    text = chunks[0]

    # Сквозная нумерация и карточки
    assert "<b>1  💰 Самый дешёвый</b>" in text
    assert "<b>2  ⚡ Самый быстрый</b>" in text
    assert flat == [o1, o2]
    assert len(pool) == 2

    # Layovers
    assert "🔁 Стамбул" in text

    # Ссылки: прямая и непрямая
    assert '<a href="https://site_b.ru/direct">купить билет</a>' in text
    assert '<a href="https://site_a.ru/1">открыть выдачу сайта</a>' in text

    # Ошибка сайта строкой
    assert "⚠️ <b>bad_site</b>: таймаут" in text

    # Ссылка на Ozon Travel и подсказка номера билета
    assert "Ozon Travel" in text
    assert "https://www.ozon.ru/travel/flight/search" in text
    assert "Напишите номер билета, чтобы увидеть подробности." in text
    assert "Цены сняты сейчас" in text

    _check_html_tags_balanced(text)


def test_compact_report_single_card():
    """Когда билет только один, показывается только одна карточка."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(site="site_a", price=10000, duration="4 ч")
    results = [SiteResult("site_a", True, [o])]

    chunks, flat, pool = build_compact_report(q, results, SearchCriteria())

    assert len(flat) == 1
    assert flat[0] is o
    assert "<b>1  💰 Самый дешёвый</b>" in chunks[0]
    assert "2. <b>" not in chunks[0]


def test_compact_report_empty_results_default_criteria():
    """Пустая выдача при дефолтных критериях: 'Билетов не нашлось', без подсказки ослабить фильтры."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    results = [SiteResult("site_a", True, [])]

    chunks, flat, pool = build_compact_report(q, results, SearchCriteria())
    text = chunks[0]

    assert "Билетов не нашлось" in text
    assert "ослабить фильтры" not in text
    assert flat == []
    assert pool == []
    assert "Ozon Travel" in text
    assert "Напишите номер билета" not in text


def test_compact_report_empty_results_non_default_criteria():
    """Пустая выдача при заданных фильтрах: подсказка ослабить фильтры."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    results = [SiteResult("site_a", True, [])]
    criteria = SearchCriteria(max_transfers=0)

    chunks, flat, pool = build_compact_report(q, results, criteria)
    text = chunks[0]

    assert "Билетов не нашлось" in text
    assert "Попробуйте ослабить фильтры" in text
    assert "⚙️ пересадки: только прямые" in text


def test_compact_report_filters_reduce_pool_and_header():
    """Шапка содержит описание фильтров и строку «скрыто фильтрами: N»."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(site="site_a", price=10000, transfers=0, duration="3 ч")
    results = [SiteResult("site_a", True, [o])]
    criteria = SearchCriteria(max_transfers=0, max_duration_hours=5)

    chunks, flat, pool = build_compact_report(
        q,
        results,
        criteria,
        baggage_mode="with",
        dropped_baggage=1,
        dropped_criteria=3,
    )
    text = chunks[0]

    assert "⚙️ пересадки: только прямые, в пути: до 5 ч, скрыто фильтрами: 3" in text
    assert "только с багажом · скрыто: 1" in text


def test_compact_report_best_balance_card_only_with_hour_cost():
    """Карточка «Лучший баланс» появляется только при заданном hour_cost."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o1 = Offer(site="s", price=10000, duration="10 ч")  # дешевый
    o2 = Offer(site="s", price=25000, duration="2 ч")   # быстрый
    o3 = Offer(site="s", price=13000, duration="3 ч")   # лучший баланс (13000 + 3*1000 = 16000)
    results = [SiteResult("s", True, [o1, o2, o3])]

    # 1. Без hour_cost -> карточки «Лучший баланс» нет
    chunks_no_hc, flat_no_hc, _ = build_compact_report(q, results, SearchCriteria(hour_cost=None))
    assert len(flat_no_hc) == 2
    assert "Лучший баланс" not in chunks_no_hc[0]

    # 2. С hour_cost -> карточка «Лучший баланс» есть
    chunks_hc, flat_hc, _ = build_compact_report(q, results, SearchCriteria(hour_cost=1000))
    assert len(flat_hc) == 3
    assert "<b>3  ⚖️ Лучший баланс</b>" in chunks_hc[0]
    assert flat_hc[2] is o3


def test_build_more_report_numbering_and_exhaustion():
    """build_more_report: продолжение нумерации, порции по 5, конец списка -> None."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    pool = [Offer(site="s", price=10000 + i * 100, offer_id=f"off_{i}") for i in range(8)]
    cards = pool[:2]  # Первые 2 уже показаны

    # 1. Первая порция: 5 билетов, номера 3-7
    text1, new1 = build_more_report(q, pool, cards, n=5, start_number=3)
    assert text1 is not None
    assert len(new1) == 5
    assert "<b>3</b>\n<b>10 200 ₽</b>" in text1
    assert "<b>7</b>\n<b>10 600 ₽</b>" in text1
    assert "<b>8</b>" not in text1

    # 2. Вторая порция: остался 1 билет, номер 8
    shown_so_far = cards + new1
    text2, new2 = build_more_report(q, pool, shown_so_far, n=5, start_number=8)
    assert text2 is not None
    assert len(new2) == 1
    assert "<b>8</b>\n<b>10 700 ₽</b>" in text2

    # 3. Конец списка -> None и []
    shown_all = shown_so_far + new2
    text3, new3 = build_more_report(q, pool, shown_all, n=5, start_number=9)
    assert text3 is None
    assert new3 == []


def test_compact_report_large_volume_chunking_le_4096():
    """Нарезка на части <= 4000 (и <= 4096) символов при большой выдаче."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    # Создадим много ошибочных сайтов, чтобы текст превысил 4000
    results = [
        SiteResult(f"site_{i}", False, [], error="очень длинная ошибка " * 10)
        for i in range(50)
    ]
    chunks, flat, pool = build_compact_report(q, results, SearchCriteria())

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4000
        assert len(chunk) <= 4096
        _check_html_tags_balanced(chunk)


@pytest.mark.asyncio
async def test_bot_flow_search_gives_compact_response_with_buttons(monkeypatch):
    """Поиск отдает компактный ответ с кнопками more, filters, full, new."""
    bot = make_fake_bot(allowed_ids={123})

    offers = [Offer(site="site1", price=5000 + i * 500, offer_id=f"id_{i}") for i in range(10)]
    async def mock_search(q, s):
        return [SiteResult("site1", True, offers)]

    monkeypatch.setattr("avia.bot.search", mock_search)

    msg = {
        "chat": {"id": 10},
        "from": {"id": 123},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(msg)

    # Проверяем отправленные сообщения
    send_calls = [p for m, p in bot.calls if m == "sendMessage" and "✈️" in p.get("text", "")]
    assert len(send_calls) == 1
    last_call = send_calls[0]
    rm = last_call.get("reply_markup")
    assert rm is not None
    kb = rm.get("inline_keyboard")

    # Проверяем кнопки
    # Ряд 1: more (так как в pool 10 билетов, а показано 1-2)
    assert any("Показать ещё" in b["text"] and b["callback_data"].startswith("more:") for b in kb[0])
    # Ряд 2: filters, full
    assert any("Фильтры" in b["text"] and b["callback_data"].startswith("filters:") for b in kb[1])
    assert any("По сайтам" in b["text"] and b["callback_data"].startswith("full:") for b in kb[1])
    # Ряд 3: hist
    assert any("История" in b["text"] and b["callback_data"].startswith("hist:") for b in kb[2])
    # Ряд 4: track, new
    assert any("Следить за ценой" in b["text"] and b["callback_data"].startswith("track:") for b in kb[3])
    assert any("Новый поиск" in b["text"] and b["callback_data"].startswith("new:") for b in kb[3])

    snap = bot.store.get(10)
    assert snap is not None
    assert snap.view == "compact"
    assert len(snap.flat) < len(snap.pool)


@pytest.mark.asyncio
async def test_bot_flow_more_extends_flat_and_opens_card(monkeypatch):
    """«more» дописывает flat и номер из второй порции открывает карточку."""
    bot = make_fake_bot(allowed_ids={123})

    offers = [
        Offer(site="site1", price=5000 + i * 500, airline=f"Air_{i}", offer_id=f"id_{i}")
        for i in range(10)
    ]
    async def mock_search(q, s):
        return [SiteResult("site1", True, offers)]

    monkeypatch.setattr("avia.bot.search", mock_search)

    # 1. Поиск
    await bot.handle({"chat": {"id": 20}, "from": {"id": 123}, "text": "/price MOW ALA 10.12"})
    snap = bot.store.get(20)
    initial_flat_len = len(snap.flat)
    session = bot.dialog.sessions[20]
    d_id = session.dialog_id

    # 2. Нажатие «more»
    bot.calls.clear()
    cq = {
        "id": "cq_more",
        "message": {"chat": {"id": 20}, "message_id": 201},
        "from": {"id": 123},
        "data": f"more:{d_id}",
    }
    await bot.handle_callback(cq)

    # Проверяем, что flat увеличился на 5
    assert len(snap.flat) == initial_flat_len + 5
    more_msg = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(more_msg) >= 1

    # 3. Запрос подробностей по билету из второй порции
    bot.calls.clear()
    target_num = initial_flat_len + 1
    await bot.handle({"chat": {"id": 20}, "from": {"id": 123}, "text": str(target_num)})

    details_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(details_calls) == 1
    details_text = details_calls[0]["text"]
    expected_airline = snap.flat[target_num - 1].airline
    assert expected_airline in details_text


@pytest.mark.asyncio
async def test_bot_flow_full_replaces_flat_and_opens_correct_ticket(monkeypatch):
    """«full» меняет flat так, что номер из полного списка открывает правильный билет."""
    bot = make_fake_bot(allowed_ids={123})

    offers_a = [Offer(site="site_a", price=10000 + i * 100, airline=f"Alpha_{i}", offer_id=f"a_{i}") for i in range(5)]
    offers_b = [Offer(site="site_b", price=5000 + i * 100, airline=f"Beta_{i}", offer_id=f"b_{i}") for i in range(5)]

    async def mock_search(q, s):
        return [SiteResult("site_a", True, offers_a), SiteResult("site_b", True, offers_b)]

    monkeypatch.setattr("avia.bot.search", mock_search)

    await bot.handle({"chat": {"id": 30}, "from": {"id": 123}, "text": "/price MOW ALA 10.12"})
    snap = bot.store.get(30)
    d_id = bot.dialog.sessions[30].dialog_id

    # 1. Нажатие «full»
    bot.calls.clear()
    cq = {
        "id": "cq_full",
        "message": {"chat": {"id": 30}, "message_id": 301},
        "from": {"id": 123},
        "data": f"full:{d_id}",
    }
    await bot.handle_callback(cq)

    assert snap.view == "full"
    # В полном списке flat содержит все 10 билетов (по 5 от каждого сайта)
    assert len(snap.flat) == 10

    # Проверяем кнопки под последним куском полного списка: «⬅ К сводке» и «🔎 Новый поиск»
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    last_call = send_calls[-1]
    rm = last_call.get("reply_markup")
    assert rm is not None
    assert any("К сводке" in b["text"] for row in rm["inline_keyboard"] for b in row)

    # 2. Ввод номера билета 5
    bot.calls.clear()
    await bot.handle({"chat": {"id": 30}, "from": {"id": 123}, "text": "5"})
    details_text = [p["text"] for m, p in bot.calls if m == "sendMessage"][0]
    expected_offer = snap.flat[4]
    assert expected_offer.airline in details_text


@pytest.mark.asyncio
async def test_bot_flow_filters_returns_baggage_with_saved_criteria(monkeypatch):
    """«filters» возвращает экран багажа с сохранёнными критериями."""
    bot = make_fake_bot(allowed_ids={123})

    async def mock_search(q, s):
        return [SiteResult("site1", True, [Offer("site1", 10000, transfers=0, offer_id="id1")])]

    monkeypatch.setattr("avia.bot.search", mock_search)

    # Запускаем поиск с критерием
    dialog = bot.dialog
    session = dialog.sessions[40] = Session(
        step=Step.BAGGAGE,
        origin="MOW",
        destination="ALA",
        depart=date(2026, 12, 10),
        adults=1,
        baggage_mode="with",
        criteria=SearchCriteria(max_transfers=0, max_duration_hours=8),
        dialog_id="init_d",
    )

    cq_bag = {
        "id": "cq_bag",
        "message": {"chat": {"id": 40}, "message_id": 401},
        "from": {"id": 123},
        "data": "bag:init_d:with",
    }
    await bot.handle_callback(cq_bag)

    snap = bot.store.get(40)
    assert snap is not None
    assert snap.criteria.max_transfers == 0
    assert snap.criteria.max_duration_hours == 8

    cur_d_id = bot.dialog.sessions[40].dialog_id

    # Нажатие «filters»
    bot.calls.clear()
    cq_filters = {
        "id": "cq_filt",
        "message": {"chat": {"id": 40}, "message_id": 402},
        "from": {"id": 123},
        "data": f"filters:{cur_d_id}",
    }
    await bot.handle_callback(cq_filters)

    new_session = bot.dialog.sessions[40]
    assert new_session.step == Step.BAGGAGE
    assert new_session.criteria.max_transfers == 0
    assert new_session.criteria.max_duration_hours == 8
    assert new_session.baggage_mode == "with"
    assert new_session.dialog_id != cur_d_id  # новый dialog_id

    send_texts = [p["text"] for m, p in bot.calls if m == "sendMessage"]
    assert any("Багаж" in t for t in send_texts)
    # На кнопке расширенных настроек отображается количество фильтров (2)
    last_call = [p for m, p in bot.calls if m == "sendMessage"][-1]
    kb = last_call["reply_markup"]["inline_keyboard"]
    adv_button = [b for row in kb for b in row if "Расширенные настройки" in b["text"]][0]
    assert "(2)" in adv_button["text"]


def test_compact_report_without_prefilled_offer_ids():
    """Билеты без prefilled offer_id получают offer_id, others вне парето не теряются."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    # o1 дешёвый и быстрый (Парето)
    o1 = Offer(site="s", price=10000, duration="3 ч", airline="SU")
    # o2 дороже и медленнее o1 (НЕ в Парето)
    o2 = Offer(site="s", price=20000, duration="5 ч", airline="S7")
    # o3 ещё дороже и медленнее (НЕ в Парето)
    o3 = Offer(site="s", price=30000, duration="8 ч", airline="UT")
    results = [SiteResult("s", True, [o1, o2, o3])]

    chunks, flat, pool = build_compact_report(q, results, SearchCriteria())

    # Все билеты должны получить offer_id
    assert all(bool(o.offer_id) for o in pool)
    assert all(bool(o.offer_id) for o in flat)

    # В pool должны быть все 3 билета: Парето (o1) + остальные (o2, o3)
    assert len(pool) == 3
    assert pool[0] is o1
    assert pool[1] is o2
    assert pool[2] is o3

    # flat содержит карточки (o1)
    assert flat == [o1]

    # build_more_report отдаёт o2 и o3, а не o1 повторно
    more_text, more_offers = build_more_report(q, pool, flat, n=5, start_number=2)
    assert more_text is not None
    assert more_offers == [o2, o3]
    assert "<b>2</b>\n<b>20 000 ₽</b>" in more_text
    assert "<b>3</b>\n<b>30 000 ₽</b>" in more_text


def test_build_more_report_int_raises_typeerror_and_set_works():
    """build_more_report отвергает int с понятной ошибкой и принимает set[str]."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    pool = [Offer(site="s", price=10000 + i * 100, offer_id=f"id_{i}") for i in range(5)]

    with pytest.raises(TypeError, match="int в shown_count не поддерживается"):
        build_more_report(q, pool, shown=1, start_number=2)

    with pytest.raises(TypeError, match="int в shown_count не поддерживается"):
        build_more_report(q, pool, shown_count=1, start_number=2)

    # set[str] идентификаторов работает
    text, offers = build_more_report(q, pool, shown={"id_0", "id_1"}, n=2, start_number=3)
    assert text is not None
    assert len(offers) == 2
    assert offers[0].offer_id == "id_2"
    assert offers[1].offer_id == "id_3"


def test_compact_report_error_spacing_no_triple_newline():
    """При наличии сайтов с ошибкой нет лишнего перевода строки (\n\n\n)."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o = Offer(site="ok_site", price=10000, duration="3 ч", offer_id="ok1")
    results = [
        SiteResult("ok_site", True, [o]),
        SiteResult("err_site", False, [], error="соединение разорвано"),
    ]
    chunks, flat, pool = build_compact_report(q, results, SearchCriteria())
    for chunk in chunks:
        assert "\n\n\n" not in chunk


@pytest.mark.asyncio
async def test_bot_flow_full_to_summary_via_list_compact(monkeypatch):
    """Возврат из полного списка к сводке («⬅ К сводке») через callback list:...:compact."""
    bot = make_fake_bot(allowed_ids={123})

    offers_a = [Offer(site="site_a", price=10000 + i * 100, airline=f"Alpha_{i}", offer_id=f"a_{i}") for i in range(5)]
    offers_b = [Offer(site="site_b", price=5000 + i * 100, airline=f"Beta_{i}", offer_id=f"b_{i}") for i in range(5)]

    async def mock_search(q, s):
        return [SiteResult("site_a", True, offers_a), SiteResult("site_b", True, offers_b)]

    monkeypatch.setattr("avia.bot.search", mock_search)

    # 1. Поиск -> компактный отчёт
    await bot.handle({"chat": {"id": 50}, "from": {"id": 123}, "text": "/price MOW ALA 10.12"})
    snap = bot.store.get(50)
    assert snap.view == "compact"
    d_id = bot.dialog.sessions[50].dialog_id

    # 2. Переход в полный список
    bot.calls.clear()
    await bot.handle_callback({
        "id": "cq_full",
        "message": {"chat": {"id": 50}, "message_id": 501},
        "from": {"id": 123},
        "data": f"full:{d_id}",
    })
    assert snap.view == "full"
    assert len(snap.flat) == 10

    # Проверяем, что в клавиатуре полного списка callback_data имеет суффикс :compact
    last_call = [p for m, p in bot.calls if m == "sendMessage"][-1]
    kb = last_call["reply_markup"]["inline_keyboard"]
    summary_btn = [b for row in kb for b in row if "К сводке" in b["text"]][0]
    assert summary_btn["callback_data"] == f"list:{d_id}:compact"

    # 3. Нажатие «⬅ К сводке» -> возвращает компактный отчёт, а не зацикливает полный список
    bot.calls.clear()
    await bot.handle_callback({
        "id": "cq_back_to_summary",
        "message": {"chat": {"id": 50}, "message_id": 502},
        "from": {"id": 123},
        "data": summary_btn["callback_data"],
    })

    assert snap.view == "compact"
    # flat сброшен обратно до карточек (не 10 билетов из полного списка)
    assert len(snap.flat) < 10
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) >= 1
    compact_text = send_calls[0]["text"]
    assert "Самый дешёвый" in compact_text

    # Проверяем кнопки компактного вида
    ret_kb = send_calls[-1]["reply_markup"]["inline_keyboard"]
    assert any("По сайтам" in b["text"] for row in ret_kb for b in row)
    assert any("Показать ещё" in b["text"] for row in ret_kb for b in row)

    # 4. Проверяем, что номер билета "1" теперь открывает именно карточку из компактного flat
    bot.calls.clear()
    await bot.handle({"chat": {"id": 50}, "from": {"id": 123}, "text": "1"})
    details_text = [p["text"] for m, p in bot.calls if m == "sendMessage"][0]
    assert snap.flat[0].airline in details_text


@pytest.mark.asyncio
async def test_bot_flow_details_back_to_compact_and_full(monkeypatch):
    """Кнопка «⬅️ К списку» из карточки деталей возвращает в правильный вид (compact или full)."""
    bot = make_fake_bot(allowed_ids={123})

    offers = [
        Offer(site="s", price=5000 + i * 500, airline=f"Air_{i}", offer_id=f"id_{i}")
        for i in range(10)
    ]
    async def mock_search(q, s):
        return [SiteResult("s", True, offers)]

    monkeypatch.setattr("avia.bot.search", mock_search)

    # 1. Поиск -> компактный отчёт
    await bot.handle({"chat": {"id": 60}, "from": {"id": 123}, "text": "/price MOW ALA 10.12"})
    snap = bot.store.get(60)
    d_id = bot.dialog.sessions[60].dialog_id

    # 2. Открываем детали из компактного отчёта
    bot.calls.clear()
    await bot.handle({"chat": {"id": 60}, "from": {"id": 123}, "text": "1"})
    details_msg = [p for m, p in bot.calls if m == "sendMessage"][0]
    kb_details = details_msg["reply_markup"]["inline_keyboard"]
    back_btn = [b for row in kb_details for b in row if "К списку" in b["text"]][0]
    assert back_btn["callback_data"] == f"list:{d_id}"

    # 3. Нажимаем «⬅️ К списку» -> возврат в компактный отчёт
    bot.calls.clear()
    await bot.handle_callback({
        "id": "cq_back_compact",
        "message": {"chat": {"id": 60}, "message_id": 601},
        "from": {"id": 123},
        "data": back_btn["callback_data"],
    })
    assert snap.view == "compact"
    last_call = [p for m, p in bot.calls if m == "sendMessage"][-1]
    ret_kb = last_call["reply_markup"]["inline_keyboard"]
    assert any("По сайтам" in b["text"] for row in ret_kb for b in row)

    # 4. Переходим в полный список
    bot.calls.clear()
    await bot.handle_callback({
        "id": "cq_full",
        "message": {"chat": {"id": 60}, "message_id": 602},
        "from": {"id": 123},
        "data": f"full:{d_id}",
    })
    assert snap.view == "full"

    # 5. Открываем детали билета номер 5 из полного списка
    bot.calls.clear()
    await bot.handle({"chat": {"id": 60}, "from": {"id": 123}, "text": "5"})
    details_msg5 = [p for m, p in bot.calls if m == "sendMessage"][0]
    kb_details5 = details_msg5["reply_markup"]["inline_keyboard"]
    back_btn5 = [b for row in kb_details5 for b in row if "К списку" in b["text"]][0]

    # 6. Нажимаем «⬅️ К списку» -> возврат в полный список, а не в компактный
    bot.calls.clear()
    await bot.handle_callback({
        "id": "cq_back_full",
        "message": {"chat": {"id": 60}, "message_id": 603},
        "from": {"id": 123},
        "data": back_btn5["callback_data"],
    })
    assert snap.view == "full"
    last_call_full = [p for m, p in bot.calls if m == "sendMessage"][-1]
    ret_kb_full = last_call_full["reply_markup"]["inline_keyboard"]
    assert any("К сводке" in b["text"] for row in ret_kb_full for b in row)


@pytest.mark.asyncio
async def test_bot_flow_expired_snapshot_handling():
    """При устаревшем снимке действия more, full, list, filters возвращают сообщение об устаревании."""
    bot = make_fake_bot(allowed_ids={123})
    chat_id = 70
    session = bot.dialog.sessions[chat_id] = Session(step=Step.DONE, dialog_id="d_test")

    for act in ["more", "full", "list", "filters"]:
        bot.calls.clear()
        cq = {
            "id": f"cq_{act}",
            "message": {"chat": {"id": chat_id}, "message_id": 700},
            "from": {"id": 123},
            "data": f"{act}:{session.dialog_id}",
        }
        await bot.handle_callback(cq)
        send_calls = [p for m, p in bot.calls if m == "sendMessage"]
        assert len(send_calls) == 1
        assert "Результаты поиска устарели" in send_calls[0]["text"]
