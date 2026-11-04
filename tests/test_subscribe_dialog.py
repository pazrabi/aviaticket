from __future__ import annotations

from datetime import date
from html import escape
import pytest

from avia.criteria import SearchCriteria
from avia.dialog import Dialog, Session, Step, compact_keyboard
from avia.formatting import money
from avia.models import Offer, Query, SiteResult
from avia.parsing import city_name
from avia.store import ResultStore
from avia.subscriptions import SubscriptionStore


@pytest.fixture
def store():
    return ResultStore()


@pytest.fixture
def subs(tmp_path):
    return SubscriptionStore(tmp_path / "test_subs.sqlite")


@pytest.fixture
def dialog(store, subs):
    return Dialog(store=store, subs=subs, today=lambda: date(2026, 11, 1))


def test_subscribe_from_results_flow(dialog, store, subs):
    """Подписка из результатов поиска: создание, стартовая цена, текст и кнопки."""
    chat_id = 100
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 1),
        return_date=date(2026, 12, 4),
        currency="RUB",
    )
    o1 = Offer(site="s1", price=9500, currency="RUB", offer_id="off1")
    o2 = Offer(site="s2", price=12000, currency="RUB", offer_id="off2")
    results = [SiteResult("s1", True, [o1]), SiteResult("s2", True, [o2])]
    store.put(chat_id, q, results, "any", [o1, o2])

    dialog.sessions[chat_id] = Session(step=Step.DONE, dialog_id="d1")
    replies = dialog.on_callback(chat_id, "track:d1")

    assert len(replies) == 1
    rep = replies[0]
    assert rep.answer == "Подписка оформлена"
    assert "Слежу за ценой" in rep.text
    assert f"Сейчас от <b>{money(9500, 'RUB')}</b>" in rep.text
    assert "Пришлю сообщение, когда цена изменится." in rep.text

    active = subs.list_active(chat_id)
    assert len(active) == 1
    sub = active[0]
    assert sub.origin == "KUF"
    assert sub.destination == "EVN"
    assert sub.depart == date(2026, 12, 1)
    assert sub.return_date == date(2026, 12, 4)
    assert subs.first_price(sub.id) == 9500

    assert rep.keyboard == [
        [("🎯 Порог цены", f"thr:{sub.id}")],
        [("🔔 Мои отслеживания", "menu:track")],
        [("🔎 Новый поиск", "menu:go")],
    ]


def test_subscribe_duplicate_not_duplicated(dialog, store, subs):
    """Повторная подписка на тот же маршрут не дублируется, содержит пометку."""
    chat_id = 101
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    o = Offer(site="s1", price=4000, currency="RUB", offer_id="off_mow")
    store.put(chat_id, q, [SiteResult("s1", True, [o])], "any", [o])

    rep1 = dialog.on_callback(chat_id, "track:d1")[0]
    assert "Вы уже следите за этим маршрутом" not in rep1.text
    assert subs.count_active(chat_id) == 1

    rep2 = dialog.on_callback(chat_id, "track:d1")[0]
    assert "Вы уже следите за этим маршрутом." in rep2.text
    assert "Слежу за ценой" in rep2.text
    assert rep2.answer == "Подписка оформлена"
    assert subs.count_active(chat_id) == 1


def test_subscribe_without_offers(dialog, store, subs):
    """Подписка при отсутствии билетов сообщает об этом и не пишет цену."""
    chat_id = 102
    q = Query(origin="KUF", destination="AER", depart=date(2026, 12, 5))
    store.put(chat_id, q, [SiteResult("s1", True, [])], "any", [])

    rep = dialog.on_callback(chat_id, "track:d1")[0]
    assert "Пока билетов нет, пришлю, когда появятся." in rep.text
    assert "Сейчас от" not in rep.text

    active = subs.list_active(chat_id)
    assert len(active) == 1
    assert subs.first_price(active[0].id) is None


def test_subscribe_with_filters_formatting(dialog, store, subs):
    """Подписка с критериями и багажом форматирует фильтры."""
    chat_id = 103
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))
    crit = SearchCriteria(max_transfers=0)
    o = Offer(site="s1", price=9500, currency="RUB")
    store.put(chat_id, q, [SiteResult("s1", True, [o])], "with", [o], criteria=crit)

    rep = dialog.on_callback(chat_id, "track:d1")[0]
    assert "с багажом, пересадки: только прямые" in rep.text


def test_subscribe_limit(dialog, store, subs):
    """Лимит активных подписок (MAX_SUBSCRIPTIONS): блокировка новой и пропуск существующей."""
    from avia.dialog import MAX_SUBSCRIPTIONS

    chat_id = 104
    for i in range(MAX_SUBSCRIPTIONS):
        q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1 + i))
        subs.create(chat_id, q, "any", SearchCriteria())

    assert subs.count_active(chat_id) == MAX_SUBSCRIPTIONS

    q_new = Query(origin="KUF", destination="AER", depart=date(2026, 12, 25))
    o = Offer(site="s1", price=5000, currency="RUB")
    store.put(chat_id, q_new, [SiteResult("s1", True, [o])], "any", [o])

    rep_limit = dialog.on_callback(chat_id, "track:d_lim")[0]
    assert f"Лимит отслеживаний: {MAX_SUBSCRIPTIONS}" in rep_limit.text
    assert rep_limit.keyboard == [[("🔔 Мои отслеживания", "menu:track")]]
    assert subs.count_active(chat_id) == MAX_SUBSCRIPTIONS

    # Повтор уже существующей подписки при полном лимите — разрешён
    q_existing = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    store.put(chat_id, q_existing, [SiteResult("s1", True, [o])], "any", [o])
    rep_dup = dialog.on_callback(chat_id, "track:d_dup")[0]
    assert "Вы уже следите за этим маршрутом." in rep_dup.text
    assert subs.count_active(chat_id) == MAX_SUBSCRIPTIONS


def test_subscriptions_unavailable_when_subs_is_none():
    """Недоступность подписок при subs=None."""
    store = ResultStore()
    d = Dialog(store=store, subs=None)
    chat_id = 105
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    store.put(chat_id, q, [], "any", [])

    # track
    rep_track = d.on_callback(chat_id, "track:d1")[0]
    assert rep_track.answer == "Отслеживание недоступно"

    # menu:track
    rep_menu = d.on_callback(chat_id, "menu:track")[0]
    assert "Отслеживание недоступно" in rep_menu.text

    # unsub
    rep_unsub = d.on_callback(chat_id, "unsub:1")[0]
    assert "Отслеживание недоступно" in rep_unsub.text

    # thr
    rep_thr = d.on_callback(chat_id, "thr:1")[0]
    assert rep_thr.answer == "Отслеживание недоступно"


def test_subscribe_snapshot_expired(dialog):
    """При устаревшем снимке поиск сообщает об этом."""
    rep = dialog.on_callback(106, "track:d1")[0]
    assert "Результаты поиска устарели, начните новый поиск" in rep.text
    assert rep.keyboard == [[("🔎 Найти билет", "menu:go")]]


def test_my_subscriptions_list_empty_and_items(dialog, subs):
    """Раздел «Мои отслеживания»: пустой и с элементами, правильный формат строк."""
    chat_id = 107
    # Пустой список
    rep_empty = dialog.on_callback(chat_id, "menu:track")[0]
    assert "Пока ничего не отслеживается" in rep_empty.text
    assert rep_empty.keyboard == [[("🔎 Найти билет", "menu:go")]]

    # Создаём подписку 1: Самара → Ереван, 01.12–04.12, 1 пасс. · пересадки: только прямые · порог 9 000 ₽ · от 9 500 ₽
    q1 = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 1),
        return_date=date(2026, 12, 4),
        adults=1,
    )
    crit1 = SearchCriteria(max_transfers=0)
    sub1 = subs.create(chat_id, q1, "any", crit1)
    subs.record_price(sub1.id, 9500, "RUB", "off1", 1000.0)
    subs.set_threshold(sub1.id, chat_id, 9000)

    # Создаём подписку 2: Москва → Сочи, 15.01, 2 пасс. · без багажа
    q2 = Query(
        origin="MOW",
        destination="AER",
        depart=date(2027, 1, 15),
        adults=2,
    )
    sub2 = subs.create(chat_id, q2, "without", SearchCriteria())

    rep_list = dialog.on_callback(chat_id, "menu:track")[0]
    normalized_text = rep_list.text.replace("\xa0", " ")
    expected_line1 = "<b>1. Самара → Ереван</b>\n📅 1 дек – 4 дек  ·  👤 1\n⚙️ пересадки: только прямые\n💰 от 9 500 ₽\n🎯 порог 9 000 ₽"
    expected_line2 = "<b>2. Москва → Сочи</b>\n📅 15 янв  ·  👤 2\n⚙️ без багажа"
    assert expected_line1 in normalized_text
    assert expected_line2 in normalized_text

    assert rep_list.keyboard == [
        [("🎯 Порог №1", f"thr:{sub1.id}"), ("🔕 Отключить №1", f"unsub:{sub1.id}")],
        [("🎯 Порог №2", f"thr:{sub2.id}"), ("🔕 Отключить №2", f"unsub:{sub2.id}")],
        [("🔎 Найти билет", "menu:go")],
    ]


def test_unsub_flow(dialog, subs):
    """Отписка: своя подписка, чужая подписка, мусорный id."""
    chat_id = 108
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    sub = subs.create(chat_id, q, "any", SearchCriteria())
    assert subs.count_active(chat_id) == 1

    # Чужая подписка
    rep_foreign = dialog.on_callback(999, f"unsub:{sub.id}")[0]
    assert rep_foreign.text == "Подписка не найдена"
    assert rep_foreign.answer == "Подписка не найдена"
    assert subs.count_active(chat_id) == 1

    # Мусорный id (не число)
    rep_garbage = dialog.on_callback(chat_id, "unsub:not_a_number")[0]
    assert rep_garbage.text == "Подписка не найдена"
    assert rep_garbage.answer == "Подписка не найдена"

    # Своя подписка
    rep_own = dialog.on_callback(chat_id, f"unsub:{sub.id}")[0]
    assert rep_own.text == "🔕 <b>Отслеживание отключено</b>"
    assert rep_own.answer == "🔕 <b>Отслеживание отключено</b>"
    assert rep_own.keyboard == [
        [("🔔 Мои отслеживания", "menu:track")],
        [("🔎 Новый поиск", "menu:go")],
    ]
    assert subs.count_active(chat_id) == 0


def test_threshold_flow(dialog, subs):
    """Порог: вход, ввод числа, ввод с ₽ и пробелами, 0 (сброс), мусор, отрицательное, отмена."""
    chat_id = 109
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    sub = subs.create(chat_id, q, "any", SearchCriteria())

    # Вход
    rep_enter = dialog.on_callback(chat_id, f"thr:{sub.id}")[0]
    assert "Порог цены" in rep_enter.text
    sess = dialog.sessions[chat_id]
    assert sess.step == Step.THRESHOLD
    assert sess.threshold_sub_id == sub.id
    assert rep_enter.keyboard == [[("✖ Отмена", f"cancel:{sess.dialog_id}")]]

    # Мусорный ввод текста
    rep_bad = dialog.on_text(chat_id, "десять тысяч")[0]
    assert "Нужно число в рублях" in rep_bad.text
    assert dialog.sessions[chat_id].step == Step.THRESHOLD

    # Отрицательное число
    rep_neg = dialog.on_text(chat_id, "-500")[0]
    assert "Нужно число в рублях" in rep_neg.text
    assert dialog.sessions[chat_id].step == Step.THRESHOLD

    # Корректный ввод с пробелами и ₽
    rep_ok = dialog.on_text(chat_id, " 9 000 ₽ ")[0]
    assert f"🎯 Порог <b>{money(9000, 'RUB')}</b> сохранён" == rep_ok.text
    assert rep_ok.keyboard == [[("🔔 Мои отслеживания", "menu:track")]]
    assert dialog.sessions[chat_id].step == Step.DONE
    assert subs.get(sub.id).threshold == 9000

    # Сброс порога вводом 0
    dialog.on_callback(chat_id, f"thr:{sub.id}")
    rep_zero = dialog.on_text(chat_id, "0 ₽")[0]
    assert rep_zero.text == "🎯 Порог убран"
    assert rep_zero.keyboard == [[("🔔 Мои отслеживания", "menu:track")]]
    assert dialog.sessions[chat_id].step == Step.DONE
    assert subs.get(sub.id).threshold is None

    # Отмена по кнопке cancel
    dialog.on_callback(chat_id, f"thr:{sub.id}")
    curr_sess = dialog.sessions[chat_id]
    rep_cancel = dialog.on_callback(chat_id, f"cancel:{curr_sess.dialog_id}")[0]
    assert "Дешёвые авиабилеты" in rep_cancel.text


def test_card_details_and_compact_keyboard_contain_track(dialog, store):
    """Карточка деталей и compact_keyboard содержат кнопку track."""
    chat_id = 110
    d_id = "test_d"
    kb = compact_keyboard(d_id, has_more=False)
    # Ряд с track отдельный
    assert ("🔔 Следить за ценой", f"track:{d_id}") in [b for row in kb for b in row]

    # Карточка деталей
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 10))
    o = Offer(site="s1", price=5000, airline="SU")
    store.put(chat_id, q, [SiteResult("s1", True, [o])], "any", [o])
    dialog.sessions[chat_id] = Session(step=Step.DONE, dialog_id=d_id)

    rep_card = dialog.on_text(chat_id, "1")[0]
    card_kb = rep_card.keyboard or []
    track_btns = [b for row in card_kb for b in row if b[1] == f"track:{d_id}"]
    assert len(track_btns) == 1
    assert track_btns[0][0] == "🔔 Следить за ценой"


def test_all_callback_data_len_le_64(dialog, store, subs):
    """Все сгенерированные callback_data <= 64 байт в UTF-8."""
    chat_id = 111
    q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1), return_date=date(2026, 12, 4))
    o = Offer(site="s1", price=9500, currency="RUB")
    store.put(chat_id, q, [SiteResult("s1", True, [o])], "any", [o])
    dialog.sessions[chat_id] = Session(step=Step.DONE, dialog_id="d_test")

    keyboards = []

    # 1. compact_keyboard
    keyboards.append(compact_keyboard("d_test", has_more=True))
    keyboards.append(compact_keyboard("d_test", has_more=False))

    # 2. track response keyboard
    rep_track = dialog.on_callback(chat_id, "track:d_test")[0]
    keyboards.append(rep_track.keyboard)

    # 3. menu:track list keyboard
    rep_list = dialog.on_callback(chat_id, "menu:track")[0]
    keyboards.append(rep_list.keyboard)

    # 4. thr prompt keyboard
    active = subs.list_active(chat_id)
    rep_thr = dialog.on_callback(chat_id, f"thr:{active[0].id}")[0]
    keyboards.append(rep_thr.keyboard)

    # 5. thr saved keyboard
    rep_saved = dialog.on_text(chat_id, "9000")[0]
    keyboards.append(rep_saved.keyboard)

    # 6. unsub response keyboard
    rep_unsub = dialog.on_callback(chat_id, f"unsub:{active[0].id}")[0]
    keyboards.append(rep_unsub.keyboard)

    for kb in keyboards:
        assert kb is not None
        for row in kb:
            for text, cb in row:
                assert len(cb.encode("utf-8")) <= 64, f"Callback {cb!r} exceeds 64 bytes"
