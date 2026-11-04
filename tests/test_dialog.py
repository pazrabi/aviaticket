from datetime import date
from html import escape

import pytest

import avia.dialog
from avia.dialog import (
    Dialog,
    Reply,
    ROUND_TRIP_SITES,
    SearchRequest,
    Session,
    Step,
)
from avia.models import Offer, Query, SiteResult
from avia.store import ResultStore

TODAY = date(2026, 10, 27)


def make_dialog() -> Dialog:
    return Dialog(today=lambda: TODAY)


def test_dialog_one_way_success():
    """Успешный путь «только туда» по шагам до SearchRequest с верным Query и baggage_mode."""
    dialog = make_dialog()
    chat_id = 101

    # 1. Открытие главного меню
    rep = dialog.on_text(chat_id, "/start")
    assert len(rep) == 1
    assert rep[0].keyboard is not None
    assert rep[0].keyboard[0][0] == ("🔎 Найти билет", "menu:go")

    # 2. Нажатие «🔎 Найти билет»
    rep = dialog.on_callback(chat_id, "menu:go")
    assert len(rep) == 1
    session = dialog.sessions[chat_id]
    assert session.step == Step.ORIGIN
    assert "Откуда" in rep[0].text
    d_id = session.dialog_id

    # 3. Ввод города вылета
    rep = dialog.on_text(chat_id, "Москва")
    assert len(rep) == 1
    assert session.step == Step.DEST
    assert session.origin == "MOW"
    assert "Куда" in rep[0].text

    # 4. Ввод города назначения
    rep = dialog.on_text(chat_id, "Алматы")
    assert len(rep) == 1
    assert session.step == Step.DATE
    assert session.destination == "ALA"
    assert "Когда вылет" in rep[0].text

    # 5. Ввод даты вылета
    rep = dialog.on_text(chat_id, "12.12.2026")
    assert len(rep) == 1
    assert session.step == Step.TRIP
    assert session.depart == date(2026, 12, 12)
    assert "Нужен обратный билет?" in rep[0].text

    # 6. Выбор «Только туда»
    rep = dialog.on_callback(chat_id, f"trip:{d_id}:one")
    assert len(rep) == 1
    assert session.step == Step.BAGGAGE
    assert session.return_date is None
    assert "Багаж" in rep[0].text

    # 7. Изменение пассажиров счётчиком: 1 -> 2
    rep = dialog.on_callback(chat_id, f"pax:{d_id}:+")
    assert len(rep) == 1
    assert rep[0].edit is True
    assert session.adults == 2
    assert session.step == Step.BAGGAGE

    # 8. Выбор багажа «С багажом» -> запуск поиска SearchRequest
    res = dialog.on_callback(chat_id, f"bag:{d_id}:with")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    sr = res[0]
    assert sr.baggage_mode == "with"
    assert sr.query == Query("MOW", "ALA", date(2026, 12, 12), adults=2, return_date=None)
    assert session.step == Step.SEARCHING


def test_dialog_round_trip_and_soon_message(monkeypatch):
    """Путь «туда-обратно» с датой возврата и сообщением про «скоро»."""
    dialog = make_dialog()
    chat_id = 102

    dialog.on_callback(chat_id, "menu:go")
    session = dialog.sessions[chat_id]
    d_id = session.dialog_id

    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    dialog.on_text(chat_id, "12.12.2026")

    # Выбор «Туда и обратно»
    rep = dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    assert len(rep) == 1
    assert session.step == Step.RETURN_DATE
    assert "Когда обратно" in rep[0].text

    # Ввод даты возврата
    rep = dialog.on_text(chat_id, "20.12.2026")
    assert len(rep) == 1
    assert session.step == Step.BAGGAGE
    assert session.return_date == date(2026, 12, 20)

    # Выбор багажа: если ни один сайт не умеет туда-обратно, показывается предупреждение
    monkeypatch.setattr(avia.dialog, "ROUND_TRIP_SITES", frozenset())
    rep = dialog.on_callback(chat_id, f"bag:{d_id}:any")
    assert len(rep) == 1
    assert isinstance(rep[0], Reply)
    assert "Обратные билеты скоро: пока ищу только в одну сторону" in rep[0].text
    assert rep[0].keyboard == [
        [("Искать только туда", f"go:{d_id}")],
        [("✖ Отмена", f"cancel:{d_id}")],
    ]

    # Нажатие «Искать только туда» запускает поиск без return_date
    res = dialog.on_callback(chat_id, f"go:{d_id}")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    sr = res[0]
    assert sr.baggage_mode == "any"
    assert sr.query.return_date is None
    assert sr.query == Query("MOW", "ALA", date(2026, 12, 12), adults=1, return_date=None)
    assert session.step == Step.SEARCHING


def test_dialog_round_trip_when_sites_support_it(monkeypatch):
    """Когда ROUND_TRIP_SITES не пуст, поиск запускается сразу с return_date."""
    import avia.dialog

    monkeypatch.setattr(avia.dialog, "ROUND_TRIP_SITES", frozenset({"mock_site"}))
    dialog = make_dialog()
    chat_id = 150

    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    dialog.on_text(chat_id, "12.12.2026")
    dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    dialog.on_text(chat_id, "20.12.2026")

    res = dialog.on_callback(chat_id, f"bag:{d_id}:with")
    assert len(res) == 1
    assert isinstance(res[0], SearchRequest)
    assert res[0].query.return_date == date(2026, 12, 20)
    assert res[0].baggage_mode == "with"



def test_dialog_cancel_on_each_step_and_command():
    """Отмена на каждом шаге и команда /cancel сбрасывают сессию и возвращают меню."""
    dialog = make_dialog()
    chat_id = 103

    # ORIGIN
    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions[chat_id].step == Step.MENU
    assert "Дешёвые авиабилеты" in rep[0].text

    # DEST
    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    dialog.on_text(chat_id, "MOW")
    assert dialog.sessions[chat_id].step == Step.DEST
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions[chat_id].step == Step.MENU

    # DATE
    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    assert dialog.sessions[chat_id].step == Step.DATE
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions[chat_id].step == Step.MENU

    # TRIP
    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    dialog.on_text(chat_id, "12.12.2026")
    assert dialog.sessions[chat_id].step == Step.TRIP
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions[chat_id].step == Step.MENU

    # RETURN_DATE
    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    dialog.on_text(chat_id, "12.12.2026")
    dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    assert dialog.sessions[chat_id].step == Step.RETURN_DATE
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions[chat_id].step == Step.MENU

    # BAGGAGE
    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    dialog.on_text(chat_id, "12.12.2026")
    dialog.on_callback(chat_id, f"trip:{d_id}:one")
    assert dialog.sessions[chat_id].step == Step.BAGGAGE
    rep = dialog.on_callback(chat_id, f"cancel:{d_id}")
    assert dialog.sessions[chat_id].step == Step.MENU

    # Команда /cancel в произвольный момент
    dialog.on_callback(chat_id, "menu:go")
    dialog.on_text(chat_id, "MOW")
    rep = dialog.on_text(chat_id, "/cancel")
    assert dialog.sessions[chat_id].step == Step.MENU
    assert "Дешёвые авиабилеты" in rep[0].text


def test_dialog_input_errors_do_not_reset_step():
    """Ошибки ввода (неизвестный город с подсказкой, прошедшая дата, возврат раньше вылета, одинаковые города) не сбрасывают шаг."""
    dialog = make_dialog()
    chat_id = 104

    dialog.on_callback(chat_id, "menu:go")
    d_id = dialog.sessions[chat_id].dialog_id

    # 1. Неизвестный город с подсказкой
    rep = dialog.on_text(chat_id, "алмати")
    assert dialog.sessions[chat_id].step == Step.ORIGIN
    assert "⚠️" in rep[0].text
    assert "не знаю город" in rep[0].text
    assert rep[0].keyboard is not None
    # Подсказка для Алматы с callback sug:<d_id>:ALA
    sug_found = False
    for row in rep[0].keyboard:
        for label, cb in row:
            if cb == f"sug:{d_id}:ALA":
                sug_found = True
                assert label == "Алматы"
    assert sug_found

    # Клик по подсказке подставляет город и переводит на шаг DEST
    rep_sug = dialog.on_callback(chat_id, f"sug:{d_id}:ALA")
    assert dialog.sessions[chat_id].step == Step.DEST
    assert dialog.sessions[chat_id].origin == "ALA"
    assert "Куда" in rep_sug[0].text

    # 2. Одинаковые города: вылет ALA, прилёт тоже ALA
    rep = dialog.on_text(chat_id, "алматы")
    assert dialog.sessions[chat_id].step == Step.DEST
    assert "⚠️" in rep[0].text
    assert "совпадают" in rep[0].text

    # Ввод корректного города назначения
    dialog.on_text(chat_id, "Москва")
    assert dialog.sessions[chat_id].step == Step.DATE
    assert dialog.sessions[chat_id].destination == "MOW"

    # 3. Дата в прошлом
    rep = dialog.on_text(chat_id, "20.10.2026")
    assert dialog.sessions[chat_id].step == Step.DATE
    assert "⚠️" in rep[0].text
    assert "в прошлом" in rep[0].text

    # Корректная дата вылета
    dialog.on_text(chat_id, "12.12.2026")
    assert dialog.sessions[chat_id].step == Step.TRIP
    dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    assert dialog.sessions[chat_id].step == Step.RETURN_DATE

    # 4. Возврат раньше вылета (10.12 раньше 12.12)
    rep = dialog.on_text(chat_id, "10.12.2026")
    assert dialog.sessions[chat_id].step == Step.RETURN_DATE
    assert "⚠️" in rep[0].text
    assert "раньше" in rep[0].text

    # Корректная дата возврата
    dialog.on_text(chat_id, "20.12.2026")
    assert dialog.sessions[chat_id].step == Step.BAGGAGE


def test_dialog_expired_button():
    """Устаревшая кнопка (чужой dialog_id или нет сессии) возвращает Reply с answer и кнопкой Найти билет."""
    dialog = make_dialog()
    chat_id = 105

    dialog.on_callback(chat_id, "menu:go")
    assert dialog.sessions[chat_id].dialog_id != "old_id"

    # Чужой dialog_id
    rep = dialog.on_callback(chat_id, "bag:old_id:with")
    assert len(rep) == 1
    assert rep[0].answer == "Диалог устарел, начните заново"
    assert "Диалог устарел, начните заново" in rep[0].text
    assert rep[0].keyboard == [[("🔎 Найти билет", "menu:go")]]

    # Нет сессии вовсе
    rep_no_sess = dialog.on_callback(99999, "bag:some_id:any")
    assert len(rep_no_sess) == 1
    assert rep_no_sess[0].answer == "Диалог устарел, начните заново"
    assert rep_no_sess[0].keyboard == [[("🔎 Найти билет", "menu:go")]]

    # Кнопки главного меню работают всегда
    rep_menu = dialog.on_callback(99999, "menu:go")
    assert "Откуда" in rep_menu[0].text
    rep_help = dialog.on_callback(99999, "menu:help")
    assert "/price" in rep_help[0].text
    rep_track = dialog.on_callback(99999, "menu:track")
    assert rep_track[0].text == "Отслеживание недоступно"


def test_dialog_all_callback_data_len_le_64(monkeypatch):
    """Все сгенерированные callback_data <= 64 байт в кодировке UTF-8."""
    dialog = make_dialog()
    chat_id = 106

    def check_keyboard(kb):
        if not kb:
            return
        for row in kb:
            for label, cb in row:
                assert len(cb.encode("utf-8")) <= 64, f"Callback {cb!r} exceeds 64 bytes"

    # Главное меню
    rep = dialog.start_menu(chat_id)
    check_keyboard(rep.keyboard)

    # Нажатие menu:go -> шаг ORIGIN
    rep = dialog.on_callback(chat_id, "menu:go")
    check_keyboard(rep[0].keyboard)

    # Ошибка с подсказками
    rep = dialog.on_text(chat_id, "самар")
    check_keyboard(rep[0].keyboard)

    # Шаг DEST
    d_id = dialog.sessions[chat_id].dialog_id
    rep = dialog.on_callback(chat_id, f"sug:{d_id}:KUF")
    check_keyboard(rep[0].keyboard)

    # Шаг DATE
    rep = dialog.on_text(chat_id, "ALA")
    check_keyboard(rep[0].keyboard)

    # Шаг TRIP
    rep = dialog.on_text(chat_id, "12.12.2026")
    check_keyboard(rep[0].keyboard)

    # Шаг RETURN_DATE
    rep = dialog.on_callback(chat_id, f"trip:{d_id}:rt")
    check_keyboard(rep[0].keyboard)

    # Шаг BAGGAGE
    rep = dialog.on_text(chat_id, "20.12.2026")
    check_keyboard(rep[0].keyboard)

    # Предупреждение о туда-обратно (когда ни один сайт не умеет)
    monkeypatch.setattr(avia.dialog, "ROUND_TRIP_SITES", frozenset())
    rep = dialog.on_callback(chat_id, f"bag:{d_id}:with")
    check_keyboard(rep[0].keyboard)

    # Карточка билета с кнопками list, track, new
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offer = Offer("site1", 10000)
    dialog.store.put(chat_id, q, [SiteResult("site1", True, [offer])], "any", [offer])
    dialog.sessions[chat_id].step = Step.DONE
    rep = dialog.on_text(chat_id, "1")
    check_keyboard(rep[0].keyboard)

    # Устаревший диалог
    rep = dialog.on_callback(chat_id, "bag:wrong:with")
    check_keyboard(rep[0].keyboard)


def test_dialog_ticket_number_flow():
    """Номер билета: верный, неверный и просроченный snapshot."""
    store = ResultStore()
    dialog = Dialog(store=store, today=lambda: TODAY)
    chat_id = 107

    q = Query("MOW", "ALA", date(2026, 12, 12))
    o1 = Offer("site1", 10000, airline="Airline1")
    o2 = Offer("site2", 12000, airline="Airline2")
    flat = [o1, o2]
    store.put(chat_id, q, [SiteResult("site1", True, flat)], "any", flat)
    dialog.sessions[chat_id] = Session(step=Step.DONE, dialog_id="d107")

    # 1. Корректный номер: карточка с деталями и кнопками
    rep = dialog.on_text(chat_id, "1")
    assert len(rep) == 1
    assert "site1" in rep[0].text
    assert "Airline1" in rep[0].text
    assert rep[0].keyboard == [
        [("⬅️ К списку", "list:d107"), ("🔔 Следить за ценой", "track:d107")],
        [("🔎 Новый поиск", "new:d107")],
    ]

    # Кнопка «🔔 Следить за ценой»
    rep_track = dialog.on_callback(chat_id, "track:d107")
    assert rep_track[0].answer == "Отслеживание недоступно"

    # Кнопка «⬅️ К списку» пересобирает build_report
    rep_list = dialog.on_callback(chat_id, "list:d107")
    assert len(rep_list) >= 1
    assert any("site1" in r.text for r in rep_list)

    # Кнопка «🔎 Новый поиск»
    rep_new = dialog.on_callback(chat_id, "new:d107")
    assert "Откуда" in rep_new[0].text
    assert dialog.sessions[chat_id].step == Step.ORIGIN

    # Вернём в Step.DONE для проверки неверных номеров
    dialog.sessions[chat_id].step = Step.DONE

    # 2. Неверный номер (0, больше длины, отрицательный)
    rep0 = dialog.on_text(chat_id, "0")
    assert rep0[0].text == "🤔 Нет варианта с таким номером. Доступно: 1–2"
    rep3 = dialog.on_text(chat_id, "3")
    assert rep3[0].text == "🤔 Нет варианта с таким номером. Доступно: 1–2"
    rep_neg = dialog.on_text(chat_id, "-1")
    assert rep_neg[0].text == "🤔 Нет варианта с таким номером. Доступно: 1–2"

    # Не число при наличии snapshot
    rep_abc = dialog.on_text(chat_id, "какой-то текст")
    assert rep_abc[0].text == "🤔 Нет варианта с таким номером. Доступно: 1–2"

    # 3. Snapshot отсутствует или истёк
    empty_chat = 108
    rep_exp = dialog.on_text(empty_chat, "1")
    assert "Результаты поиска устарели, начните новый поиск" in rep_exp[0].text
    assert rep_exp[0].keyboard == [[("🔎 Найти билет", "menu:go")]]


def test_dialog_number_on_step_is_not_offer_number():
    """Число на шаге диалога не считается номером билета."""
    store = ResultStore()
    dialog = Dialog(store=store, today=lambda: TODAY)
    chat_id = 109

    # Создаём снимок в store
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offer = Offer("site1", 10000)
    store.put(chat_id, q, [SiteResult("site1", True, [offer])], "any", [offer])

    # Начинаем диалог: шаг ORIGIN
    dialog.on_callback(chat_id, "menu:go")
    assert dialog.sessions[chat_id].step == Step.ORIGIN

    # Пользователь отправляет «1» на шаге ORIGIN
    rep = dialog.on_text(chat_id, "1")
    assert dialog.sessions[chat_id].step == Step.ORIGIN
    assert "⚠️" in rep[0].text
    assert "site1" not in rep[0].text  # не показал карточку билета

    # Доходим до шага DATE
    dialog.on_text(chat_id, "MOW")
    dialog.on_text(chat_id, "ALA")
    assert dialog.sessions[chat_id].step == Step.DATE

    # Пользователь отправляет «1» на шаге DATE
    rep = dialog.on_text(chat_id, "1")
    assert dialog.sessions[chat_id].step == Step.DATE
    assert "⚠️" in rep[0].text
    assert "site1" not in rep[0].text
