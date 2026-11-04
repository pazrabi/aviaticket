from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from html import escape
import secrets
import time

from .criteria import (
    ALLOWED_DAY_PARTS,
    ALLOWED_MAX_TRANSFERS,
    DAY_PARTS,
    SearchCriteria,
)
from .formatting import (
    build_compact_report,
    build_more_report,
    build_report,
    date_ru,
    format_details,
    money,
)
from .models import Offer, Query
from .parsing import (
    ParseError,
    UnknownCityError,
    city_name,
    parse_date,
    parse_passengers,
    parse_query,
    parse_return_date,
    suggest_cities,
    to_iata,
)
from .store import ResultStore, SearchSnapshot
from .subscriptions import SubscriptionStore

MAX_SUBSCRIPTIONS = 1

# сайты, умеющие «туда и обратно» (все пять сайтов); пустое множество включает заглушку «скоро»
ROUND_TRIP_SITES: frozenset[str] = frozenset({"aviasales", "kupibilet", "yandex_travel", "tutu", "onetwotrip", "ozon_travel"})

MENU_TEXT = (
    "✈️ <b>Дешёвые авиабилеты</b>\n"
    "Сравниваю цены на нескольких сервисах и показываю самое выгодное.\n\n"
    "Нажмите «Найти билет» — и ответьте на пару вопросов."
)

HELP = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "1. Нажмите «🔎 Найти билет» и ответьте на вопросы: откуда, куда, даты, багаж.\n"
    "2. Бот опросит сервисы и покажет самый дешёвый и самый быстрый варианты.\n"
    "3. Напишите номер билета, чтобы увидеть подробности.\n"
    "4. «🔔 Отслеживать цену» — пришлю сообщение, когда цена изменится.\n\n"
    "⚡ <b>Быстрый поиск командой</b>\n"
    "<code>/price Москва Алматы 10.12</code>\n"
    "<code>/price MOW TAS 2026-12-10 2</code> — 2 пассажира\n\n"
    "Города можно писать по-русски или кодом IATA."
)


class Step(str, Enum):
    MENU = "MENU"
    ORIGIN = "ORIGIN"
    DEST = "DEST"
    DATE = "DATE"
    TRIP = "TRIP"
    RETURN_DATE = "RETURN_DATE"
    BAGGAGE = "BAGGAGE"
    SEARCHING = "SEARCHING"
    DONE = "DONE"
    THRESHOLD = "THRESHOLD"


@dataclass
class Reply:
    text: str = ""
    keyboard: list[list[tuple[str, str]]] | None = None
    edit: bool = False
    answer: str | None = None


@dataclass
class SearchRequest:
    query: Query
    baggage_mode: str = "any"
    criteria: SearchCriteria = field(default_factory=SearchCriteria)
    dialog_id: str | None = None


@dataclass
class HistoryRequest:
    query: Query


@dataclass
class Session:
    step: Step = Step.MENU
    origin: str | None = None
    destination: str | None = None
    depart: date | None = None
    return_date: date | None = None
    adults: int = 1
    baggage_mode: str = "any"
    dialog_id: str = field(default_factory=lambda: secrets.token_hex(2))
    criteria: SearchCriteria = field(default_factory=SearchCriteria)
    threshold_sub_id: int | None = None


def to_reply_markup(keyboard: list[list[tuple[str, str]]] | None) -> dict | None:
    if not keyboard:
        return None
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": cb} for text, cb in row]
            for row in keyboard
        ]
    }


def compact_keyboard(dialog_id: str, *, has_more: bool) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    if has_more:
        rows.append([("➕ Показать ещё", f"more:{dialog_id}")])
    rows.append([
        ("⚙️ Фильтры", f"filters:{dialog_id}"),
        ("📋 По сайтам", f"full:{dialog_id}"),
    ])
    rows.append([("📊 История", f"hist:{dialog_id}")])
    rows.append([
        ("🔔 Следить за ценой", f"track:{dialog_id}"),
        ("🔎 Новый поиск", f"new:{dialog_id}"),
    ])
    return rows


def _baggage_text(session: Session) -> str:
    lines = ["🧳 <b>Багаж</b>"]
    if session.origin and session.destination and session.depart:
        route = f"{escape(city_name(session.origin))} → {escape(city_name(session.destination))}"
        dates = f"{date_ru(session.depart)}"
        if session.return_date:
            dates += f" – {date_ru(session.return_date)}"
        lines.insert(0, f"✈️ <b>{route}</b>  ·  📅 {dates}\n")
    lines.append("Какие билеты показывать?")
    return "\n".join(lines)


def _format_filters(baggage_mode: str, criteria: SearchCriteria) -> list[str]:
    items: list[str] = []
    if baggage_mode == "with":
        items.append("с багажом")
    elif baggage_mode == "without":
        items.append("без багажа")
    items.extend(criteria.describe())
    return items


def full_keyboard(dialog_id: str) -> list[list[tuple[str, str]]]:
    return [
        [
            ("⬅️ К сводке", f"list:{dialog_id}:compact"),
            ("🔎 Новый поиск", f"new:{dialog_id}"),
        ]
    ]


class Dialog:
    def __init__(
        self,
        sessions: dict[int | str, Session] | None = None,
        store: ResultStore | None = None,
        today: Callable[[], date] | None = None,
        subs: SubscriptionStore | None = None,
    ):
        self.sessions: dict[int | str, Session] = sessions if sessions is not None else {}
        self.store: ResultStore = store if store is not None else ResultStore()
        self.today: Callable[[], date] = today if today is not None else date.today
        self.subs: SubscriptionStore | None = subs

    def start_menu(self, chat_id: int | str) -> Reply:
        self.sessions[chat_id] = Session(step=Step.MENU, dialog_id=secrets.token_hex(2))
        return Reply(
            text=MENU_TEXT,
            keyboard=[
                [("🔎 Найти билет", "menu:go")],
                [("🔔 Мои отслеживания", "menu:track")],
                [("ℹ️ Помощь", "menu:help")],
            ],
        )

    def search_finished(self, chat_id: int | str, dialog_id: str | None = None) -> Session | None:
        """Завершает поиск: переводит step в DONE, если сессия совпадает с dialog_id."""
        session = self.sessions.get(chat_id)
        if session is None:
            return None
        if dialog_id is not None and session.dialog_id != dialog_id:
            return None
        if session.step == Step.SEARCHING:
            session.step = Step.DONE
        return session

    def _start_search(self, chat_id: int | str) -> Reply:
        session = Session(step=Step.ORIGIN, dialog_id=secrets.token_hex(2))
        self.sessions[chat_id] = session
        return Reply(
            text="🛫 <b>Откуда летим?</b>\nГород или код IATA, например <i>Москва</i> или <i>MOW</i>",
            keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
        )

    def _baggage_reply(self, session: Session, *, edit: bool = False) -> Reply:
        desc = session.criteria.describe()
        adv_text = f"⚙️ Расширенные настройки ({len(desc)})" if desc else "⚙️ Расширенные настройки"
        return Reply(
            text=_baggage_text(session),
            keyboard=[
                [
                    ("🧳 С багажом", f"bag:{session.dialog_id}:with"),
                    ("🎒 Без багажа", f"bag:{session.dialog_id}:without"),
                ],
                [("✅ Не важно — показать всё", f"bag:{session.dialog_id}:any")],
                [(f"👤 Пассажиров: {session.adults}  ·  изменить", f"pax:{session.dialog_id}:+")],
                [(adv_text, f"adv:{session.dialog_id}:menu")],
                [("✖ Отмена", f"cancel:{session.dialog_id}")],
            ],
            edit=edit,
        )

    def _adv_menu_reply(self, session: Session) -> Reply:
        desc = session.criteria.describe()
        body = "\n".join(f"• {d}" for d in desc) if desc else "Фильтры не заданы — показываю всё"
        text = f"⚙️ <b>Расширенные настройки</b>\n\n{escape(body)}"
        return Reply(
            text=text,
            keyboard=[
                [("🔀 Пересадки", f"adv:{session.dialog_id}:s_tr")],
                [("⏱ Время в пути", f"adv:{session.dialog_id}:s_dur")],
                [("🛫 Время вылета", f"adv:{session.dialog_id}:s_dp")],
                [("🛬 Время прилёта", f"adv:{session.dialog_id}:s_ap")],
                [("⚖️ Баланс цены и времени", f"adv:{session.dialog_id}:s_hc")],
                [("🧹 Сбросить всё", f"adv:{session.dialog_id}:reset")],
                [("✅ Готово", f"adv:{session.dialog_id}:done")],
                [("✖ Отмена", f"cancel:{session.dialog_id}")],
            ],
            edit=True,
        )

    def _adv_transfers_reply(self, session: Session) -> Reply:
        return Reply(
            text="🔀 <b>Сколько пересадок допустимо?</b>",
            keyboard=[
                [("✈️ Только прямые", f"adv:{session.dialog_id}:tr_0")],
                [("До 1 пересадки", f"adv:{session.dialog_id}:tr_1")],
                [("До 2 пересадок", f"adv:{session.dialog_id}:tr_2")],
                [("Не важно", f"adv:{session.dialog_id}:tr_any")],
                [("⬅️ Назад", f"adv:{session.dialog_id}:menu")],
            ],
            edit=True,
        )

    def _adv_duration_reply(self, session: Session) -> Reply:
        return Reply(
            text="⏱ <b>Максимальное время в пути</b>",
            keyboard=[
                [("До 8 ч", f"adv:{session.dialog_id}:dur_8")],
                [("До 15 ч", f"adv:{session.dialog_id}:dur_15")],
                [("До 24 ч", f"adv:{session.dialog_id}:dur_24")],
                [("Не важно", f"adv:{session.dialog_id}:dur_any")],
                [("⬅️ Назад", f"adv:{session.dialog_id}:menu")],
            ],
            edit=True,
        )

    def _adv_depart_reply(self, session: Session) -> Reply:
        labels = {
            "morning": "🌅 утро 06–12",
            "day": "☀️ день 12–18",
            "evening": "🌆 вечер 18–24",
            "night": "🌙 ночь 00–06",
        }
        rows: list[list[tuple[str, str]]] = []
        for part in DAY_PARTS:
            lbl = labels.get(part, part)
            if part in session.criteria.depart_parts:
                lbl = f"✅ {lbl}"
            rows.append([(lbl, f"adv:{session.dialog_id}:dp_{part}")])
        rows.append([("Не важно", f"adv:{session.dialog_id}:dp_any")])
        rows.append([("⬅️ Назад", f"adv:{session.dialog_id}:menu")])
        return Reply(text="🛫 <b>Когда вылетать?</b>\nМожно выбрать несколько вариантов", keyboard=rows, edit=True)

    def _adv_arrive_reply(self, session: Session) -> Reply:
        labels = {
            "morning": "🌅 утро 06–12",
            "day": "☀️ день 12–18",
            "evening": "🌆 вечер 18–24",
            "night": "🌙 ночь 00–06",
        }
        rows: list[list[tuple[str, str]]] = []
        for part in DAY_PARTS:
            lbl = labels.get(part, part)
            if part in session.criteria.arrive_parts:
                lbl = f"✅ {lbl}"
            rows.append([(lbl, f"adv:{session.dialog_id}:ap_{part}")])
        rows.append([("Не важно", f"adv:{session.dialog_id}:ap_any")])
        rows.append([("⬅️ Назад", f"adv:{session.dialog_id}:menu")])
        return Reply(text="🛬 <b>Когда прилетать?</b>\nМожно выбрать несколько вариантов", keyboard=rows, edit=True)

    def _adv_balance_reply(self, session: Session) -> Reply:
        text = (
            "⚖️ <b>Баланс цены и времени</b>\n\n"
            "Считаю цену плюс стоимость часа в пути и показываю отдельную карточку «Лучший баланс»."
        )
        keyboard = [
            [("500 ₽/ч", f"adv:{session.dialog_id}:hc_500")],
            [("1000 ₽/ч", f"adv:{session.dialog_id}:hc_1000")],
            [("2000 ₽/ч", f"adv:{session.dialog_id}:hc_2000")],
            [("Выключить", f"adv:{session.dialog_id}:hc_off")],
            [("⬅️ Назад", f"adv:{session.dialog_id}:menu")],
        ]
        return Reply(text=text, keyboard=keyboard, edit=True)

    def _on_adv_callback(self, session: Session, value: str) -> Reply:
        if value == "menu":
            return self._adv_menu_reply(session)
        if value == "s_tr":
            return self._adv_transfers_reply(session)
        if value == "s_dur":
            return self._adv_duration_reply(session)
        if value == "s_dp":
            return self._adv_depart_reply(session)
        if value == "s_ap":
            return self._adv_arrive_reply(session)
        if value == "s_hc":
            return self._adv_balance_reply(session)
        if value == "reset":
            session.criteria = SearchCriteria()
            return self._adv_menu_reply(session)
        if value == "done":
            return self._baggage_reply(session, edit=True)

        if value == "tr_any":
            session.criteria = replace(session.criteria, max_transfers=None)
            return self._adv_menu_reply(session)
        if value.startswith("tr_"):
            try:
                num = int(value.removeprefix("tr_"))
                if num in ALLOWED_MAX_TRANSFERS:
                    session.criteria = replace(session.criteria, max_transfers=num)
                    return self._adv_menu_reply(session)
            except ValueError:
                pass
            return self._adv_menu_reply(session)

        if value == "dur_any":
            session.criteria = replace(session.criteria, max_duration_hours=None)
            return self._adv_menu_reply(session)
        if value.startswith("dur_"):
            try:
                hours = int(value.removeprefix("dur_"))
                if hours in (8, 15, 24):
                    session.criteria = replace(session.criteria, max_duration_hours=hours)
                    return self._adv_menu_reply(session)
            except ValueError:
                pass
            return self._adv_menu_reply(session)

        if value == "dp_any":
            session.criteria = replace(session.criteria, depart_parts=frozenset())
            return self._adv_depart_reply(session)
        if value.startswith("dp_"):
            part = value.removeprefix("dp_")
            if part in ALLOWED_DAY_PARTS:
                cur = set(session.criteria.depart_parts)
                if part in cur:
                    cur.remove(part)
                else:
                    cur.add(part)
                session.criteria = replace(session.criteria, depart_parts=frozenset(cur))
                return self._adv_depart_reply(session)
            return self._adv_menu_reply(session)

        if value == "ap_any":
            session.criteria = replace(session.criteria, arrive_parts=frozenset())
            return self._adv_arrive_reply(session)
        if value.startswith("ap_"):
            part = value.removeprefix("ap_")
            if part in ALLOWED_DAY_PARTS:
                cur = set(session.criteria.arrive_parts)
                if part in cur:
                    cur.remove(part)
                else:
                    cur.add(part)
                session.criteria = replace(session.criteria, arrive_parts=frozenset(cur))
                return self._adv_arrive_reply(session)
            return self._adv_menu_reply(session)

        if value in ("hc_off", "hc_any"):
            session.criteria = replace(session.criteria, hour_cost=None)
            return self._adv_menu_reply(session)
        if value.startswith("hc_"):
            try:
                cost = int(value.removeprefix("hc_"))
                if cost in (500, 1000, 2000):
                    session.criteria = replace(session.criteria, hour_cost=cost)
                    return self._adv_menu_reply(session)
            except ValueError:
                pass
            return self._adv_menu_reply(session)

        return self._adv_menu_reply(session)

    def _sug_code(self, sugg: str) -> str:
        try:
            return to_iata(sugg)
        except Exception:
            return sugg[:10]

    def _expired_reply(self) -> Reply:
        return Reply(
            text="⌛ Диалог устарел, начните заново",
            keyboard=[[("🔎 Найти билет", "menu:go")]],
            answer="Диалог устарел, начните заново",
        )

    def _my_subscriptions(self, chat_id: int | str) -> list[Reply]:
        if self.subs is None:
            return [
                Reply(
                    text="Отслеживание недоступно",
                    keyboard=[[("🔎 Найти билет", "menu:go")]],
                )
            ]
        subs = self.subs.list_active(int(chat_id))
        if not subs:
            return [
                Reply(
                    text="🔔 <b>Пока ничего не отслеживается</b>\nВыполните поиск и нажмите «🔔 Следить за ценой»",
                    keyboard=[[("🔎 Найти билет", "menu:go")]],
                )
            ]

        lines: list[str] = []
        keyboard: list[list[tuple[str, str]]] = []
        for idx, sub in enumerate(subs, 1):
            dates_str = date_ru(sub.depart)
            if sub.return_date:
                dates_str += f" – {date_ru(sub.return_date)}"

            orig_name = escape(city_name(sub.origin))
            dest_name = escape(city_name(sub.destination))
            block = [
                f"<b>{idx}. {orig_name} → {dest_name}</b>",
                f"📅 {dates_str}  ·  👤 {sub.adults}",
            ]
            filters = _format_filters(sub.baggage_mode, sub.criteria)
            if filters:
                block.append(f"⚙️ {escape(', '.join(filters))}")
            if sub.min_price is not None:
                block.append(f"💰 от {money(sub.min_price, 'RUB')}")
            if sub.threshold is not None:
                block.append(f"🎯 порог {money(sub.threshold, 'RUB')}")
            lines.append("\n".join(block))
            keyboard.append([
                (f"🎯 Порог №{idx}", f"thr:{sub.id}"),
                (f"🔕 Отключить №{idx}", f"unsub:{sub.id}"),
            ])

        keyboard.append([("🔎 Найти билет", "menu:go")])
        return [Reply(text="🔔 <b>Мои отслеживания</b>\n\n" + "\n\n".join(lines), keyboard=keyboard)]

    def _on_unsub_callback(self, chat_id: int | str, sub_id_str: str) -> list[Reply]:
        if self.subs is None:
            return [
                Reply(
                    text="Отслеживание недоступно",
                    answer="Отслеживание недоступно",
                    keyboard=[
                        [("🔔 Мои отслеживания", "menu:track")],
                        [("🔎 Новый поиск", "menu:go")],
                    ],
                )
            ]
        try:
            sub_id = int(sub_id_str)
        except (ValueError, TypeError):
            sub_id = None

        if sub_id is not None and self.subs.deactivate(sub_id, int(chat_id)):
            msg = "🔕 <b>Отслеживание отключено</b>"
        else:
            msg = "Подписка не найдена"

        return [
            Reply(
                text=msg,
                answer=msg,
                keyboard=[
                    [("🔔 Мои отслеживания", "menu:track")],
                    [("🔎 Новый поиск", "menu:go")],
                ],
            )
        ]

    def _on_thr_callback(self, chat_id: int | str, sub_id_str: str) -> list[Reply]:
        if self.subs is None:
            return [Reply(answer="Отслеживание недоступно")]
        try:
            sub_id = int(sub_id_str)
        except (ValueError, TypeError):
            sub_id = None

        sub = self.subs.get(sub_id) if sub_id is not None else None
        if sub is None or not sub.active or sub.chat_id != int(chat_id):
            return [
                Reply(
                    text="Подписка не найдена",
                    answer="Подписка не найдена",
                    keyboard=[
                        [("🔔 Мои отслеживания", "menu:track")],
                        [("🔎 Новый поиск", "menu:go")],
                    ],
                )
            ]

        session = Session(
            step=Step.THRESHOLD,
            dialog_id=secrets.token_hex(2),
            threshold_sub_id=sub.id,
        )
        self.sessions[chat_id] = session
        return [
            Reply(
                text="🎯 <b>Порог цены</b>\nНапишите сумму в рублях — сообщу, когда цена станет ниже.\nЧтобы убрать порог, отправьте 0.",
                keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
            )
        ]

    def _on_track_callback(self, chat_id: int | str) -> list[Reply]:
        snap = self.store.get(chat_id)
        if snap is None:
            return [
                Reply(
                    text="⌛ Результаты поиска устарели, начните новый поиск",
                    keyboard=[[("🔎 Найти билет", "menu:go")]],
                )
            ]
        if self.subs is None:
            return [Reply(answer="Отслеживание недоступно")]

        active_subs = self.subs.list_active(int(chat_id))
        existing = next(
            (
                s
                for s in active_subs
                if (
                    s.origin == snap.query.origin
                    and s.destination == snap.query.destination
                    and s.depart == snap.query.depart
                    and s.return_date == snap.query.return_date
                    and s.adults == snap.query.adults
                    and s.baggage_mode == snap.baggage_mode
                    and s.criteria == snap.criteria
                )
            ),
            None,
        )
        if existing is None and len(active_subs) >= MAX_SUBSCRIPTIONS:
            return [
                Reply(
                    text=f"⚠️ Лимит отслеживаний: {MAX_SUBSCRIPTIONS}. Отключите ненужное, чтобы добавить новое.",
                    keyboard=[[("🔔 Мои отслеживания", "menu:track")]],
                )
            ]

        now = time.time()
        if existing is not None:
            sub = existing
            already_exists = True
        else:
            sub = self.subs.create(
                int(chat_id),
                snap.query,
                snap.baggage_mode,
                snap.criteria,
                now=now,
            )
            already_exists = False

        matching_offers = [
            o
            for r in snap.results
            for o in r.offers
            if o.price is not None and o.currency == snap.query.currency
        ]
        best_offer = min(matching_offers, key=lambda o: o.price) if matching_offers else None

        if best_offer is not None and (not already_exists or self.subs.first_price(sub.id) is None):
            self.subs.record_price(
                sub.id,
                best_offer.price,
                best_offer.currency,
                best_offer.offer_id or None,
                now,
            )

        orig_city = city_name(snap.query.origin)
        dest_city = city_name(snap.query.destination)
        dates_str = date_ru(snap.query.depart)
        if snap.query.return_date:
            dates_str += f" – {date_ru(snap.query.return_date)}"

        lines = [
            "🔔 <b>Слежу за ценой</b>",
            "",
            f"✈️ <b>{escape(orig_city)} → {escape(dest_city)}</b>",
            f"📅 {dates_str}",
        ]
        filters = _format_filters(snap.baggage_mode, snap.criteria)
        if filters:
            lines.append(f"⚙️ {escape(', '.join(filters))}")
        lines.append("")
        if best_offer is not None:
            lines.append(f"Сейчас от <b>{money(best_offer.price, best_offer.currency)}</b>")
            lines.append("Пришлю сообщение, когда цена изменится.")
        else:
            lines.append("Пока билетов нет, пришлю, когда появятся.")
        if already_exists:
            lines.append("")
            lines.append("<i>Вы уже следите за этим маршрутом.</i>")
        text = "\n".join(lines)

        keyboard = [
            [("🎯 Порог цены", f"thr:{sub.id}")],
            [("🔔 Мои отслеживания", "menu:track")],
            [("🔎 Новый поиск", "menu:go")],
        ]
        return [Reply(text=text, keyboard=keyboard, answer="Подписка оформлена")]

    def _on_hist_callback(self, chat_id: int | str, parts: list[str]) -> list[Reply | HistoryRequest]:
        if len(parts) >= 3 and parts[1] == "sub":
            sub_id_str = parts[2]
            if self.subs is not None:
                try:
                    sub = self.subs.get(int(sub_id_str))
                    if sub is not None and sub.chat_id == int(chat_id):
                        return [HistoryRequest(query=sub.query())]
                except (ValueError, TypeError):
                    pass
            return [Reply(text="Пока мало данных по этому маршруту, загляните позже")]

        q: Query | None = None
        snap = self.store.get(chat_id)
        if snap is not None:
            q = snap.query
        else:
            session = self.sessions.get(chat_id)
            if session is not None and session.origin and session.destination and session.depart:
                q = Query(
                    origin=session.origin,
                    destination=session.destination,
                    depart=session.depart,
                    return_date=session.return_date,
                    adults=session.adults,
                )

        if q is not None:
            return [HistoryRequest(query=q)]
        return [Reply(text="Пока мало данных по этому маршруту, загляните позже")]

    def on_callback(self, chat_id: int | str, data: str) -> list[Reply | SearchRequest | HistoryRequest]:
        if data == "menu:go":
            return [self._start_search(chat_id)]
        if data == "menu:track":
            return self._my_subscriptions(chat_id)
        if data == "menu:help":
            return [
                Reply(
                    text=HELP,
                    answer="Помощь",
                    keyboard=[[("🔎 Найти билет", "menu:go")]],
                )
            ]
        if data == "menu:menu":
            return [self.start_menu(chat_id)]

        parts = data.split(":")
        action = parts[0]
        sub_id_or_dialog_id = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""

        if action == "unsub":
            return self._on_unsub_callback(chat_id, sub_id_or_dialog_id)

        if action == "thr":
            return self._on_thr_callback(chat_id, sub_id_or_dialog_id)

        if action == "track":
            return self._on_track_callback(chat_id)

        if action == "hist":
            return self._on_hist_callback(chat_id, parts)

        cb_dialog_id = sub_id_or_dialog_id
        session = self.sessions.get(chat_id)
        if session is None or session.dialog_id != cb_dialog_id:
            return [self._expired_reply()]

        if action == "cancel":
            self.sessions.pop(chat_id, None)
            return [self.start_menu(chat_id)]

        if action == "sug":
            iata = value
            if session.step == Step.ORIGIN:
                session.origin = iata
                session.step = Step.DEST
                return [
                    Reply(
                        text="🛬 <b>Куда летим?</b>\nГород или код IATA, например <i>Ереван</i> или <i>EVN</i>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            elif session.step == Step.DEST:
                if iata == session.origin:
                    return [
                        Reply(
                            text=f"⚠️ {escape('города вылета и прилёта совпадают')}\n\n🛬 <b>Куда летим?</b>\nГород или код IATA, например <i>Ереван</i> или <i>EVN</i>",
                            keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                        )
                    ]
                session.destination = iata
                session.step = Step.DATE
                return [
                    Reply(
                        text="📅 <b>Когда вылет?</b>\nНапример: <code>12.12</code>, <code>12.12.2026</code> или <code>2026-12-12</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            return []

        if action == "trip":
            if value == "one":
                session.return_date = None
                session.step = Step.BAGGAGE
                return [self._baggage_reply(session, edit=False)]
            elif value == "rt":
                session.step = Step.RETURN_DATE
                return [
                    Reply(
                        text="📅 <b>Когда обратно?</b>\nНапример: <code>20.12</code> или <code>20.12.2026</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            return []

        if action == "pax":
            if session.step == Step.BAGGAGE:
                session.adults = (session.adults % 9) + 1
                return [self._baggage_reply(session, edit=True)]
            return []

        if action == "adv":
            if session.step != Step.BAGGAGE:
                return []
            if value == "cancel":
                self.sessions.pop(chat_id, None)
                return [self.start_menu(chat_id)]
            return [self._on_adv_callback(session, value)]

        if action == "bag":
            mode = value if value in {"with", "without", "any"} else "any"
            session.baggage_mode = mode
            if session.return_date is not None and not ROUND_TRIP_SITES:
                return [
                    Reply(
                        text="↔️ Обратные билеты скоро: пока ищу только в одну сторону",
                        keyboard=[
                            [("Искать только туда", f"go:{session.dialog_id}")],
                            [("✖ Отмена", f"cancel:{session.dialog_id}")],
                        ],
                        edit=True,
                    )
                ]
            session.step = Step.SEARCHING
            q = Query(
                origin=session.origin or "",
                destination=session.destination or "",
                depart=session.depart or self.today(),
                adults=session.adults,
                return_date=session.return_date,
            )
            return [
                SearchRequest(
                    query=q,
                    baggage_mode=session.baggage_mode,
                    criteria=session.criteria,
                    dialog_id=session.dialog_id,
                )
            ]

        if action == "go":
            session.return_date = None
            session.step = Step.SEARCHING
            q = Query(
                origin=session.origin or "",
                destination=session.destination or "",
                depart=session.depart or self.today(),
                adults=session.adults,
                return_date=None,
            )
            return [
                SearchRequest(
                    query=q,
                    baggage_mode=session.baggage_mode,
                    criteria=session.criteria,
                    dialog_id=session.dialog_id,
                )
            ]

        if action == "new":
            return [self._start_search(chat_id)]

        if action == "more":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="⌛ Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            text, new_offers = build_more_report(
                snap.query,
                snap.pool,
                snap.flat,
                n=5,
                start_number=len(snap.flat) + 1,
            )
            if not text or not new_offers:
                return []
            snap.flat.extend(new_offers)
            snap.shown += len(new_offers)
            shown_ids = {o.offer_id for o in snap.flat if o.offer_id}
            has_more = any(o.offer_id not in shown_ids for o in snap.pool)
            kb = compact_keyboard(session.dialog_id, has_more=has_more)
            return [Reply(text=text, keyboard=kb, edit=False)]

        if action == "full":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="⌛ Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            chunks, full_flat = build_report(
                snap.query,
                snap.results,
                baggage_mode=snap.baggage_mode,
                dropped=snap.dropped_baggage,
            )
            snap.flat = full_flat
            snap.view = "full"
            kb = full_keyboard(session.dialog_id)
            replies = [Reply(text=chunk, edit=False) for chunk in chunks[:-1]]
            replies.append(Reply(text=chunks[-1], keyboard=kb, edit=False))
            return replies

        if action == "list":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="⌛ Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            target_view = "compact" if (value == "compact" or snap.view == "compact") else "full"
            if target_view == "compact":
                snap.view = "compact"
                chunks, compact_flat, pool = build_compact_report(
                    snap.query,
                    snap.results,
                    snap.criteria,
                    baggage_mode=snap.baggage_mode,
                    dropped_baggage=snap.dropped_baggage,
                    dropped_criteria=snap.dropped_criteria,
                )
                snap.flat = compact_flat
                snap.pool = pool
                snap.shown = len(compact_flat)
                shown_ids = {o.offer_id for o in compact_flat if o.offer_id}
                has_more = any(o.offer_id not in shown_ids for o in pool)
                kb = compact_keyboard(session.dialog_id, has_more=has_more)
                replies = [Reply(text=chunk, edit=False) for chunk in chunks[:-1]]
                replies.append(Reply(text=chunks[-1], keyboard=kb, edit=False))
                return replies
            else:
                snap.view = "full"
                chunks, full_flat = build_report(
                    snap.query,
                    snap.results,
                    baggage_mode=snap.baggage_mode,
                    dropped=snap.dropped_baggage,
                )
                snap.flat = full_flat
                kb = full_keyboard(session.dialog_id)
                replies = [Reply(text=chunk, edit=False) for chunk in chunks[:-1]]
                replies.append(Reply(text=chunks[-1], keyboard=kb, edit=False))
                return replies

        if action == "filters":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="⌛ Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            new_session = Session(
                step=Step.BAGGAGE,
                origin=snap.query.origin,
                destination=snap.query.destination,
                depart=snap.query.depart,
                return_date=snap.query.return_date,
                adults=snap.query.adults,
                baggage_mode=snap.baggage_mode,
                criteria=snap.criteria,
                dialog_id=secrets.token_hex(2),
            )
            self.sessions[chat_id] = new_session
            return [self._baggage_reply(new_session, edit=False)]

        return []

    def on_text(self, chat_id: int | str, text: str) -> list[Reply | SearchRequest]:
        text = text.strip()

        if text.startswith(("/start", "/menu")):
            return [self.start_menu(chat_id)]

        if text == "/cancel":
            self.sessions.pop(chat_id, None)
            return [self.start_menu(chat_id)]

        if text == "/help":
            return [Reply(text=HELP, keyboard=[[("🔎 Найти билет", "menu:go")]])]

        if text.startswith("/price"):
            args = text.removeprefix("/price").strip()
            try:
                q = parse_query(args, today=self.today())
                d_id = secrets.token_hex(2)
                self.sessions[chat_id] = Session(
                    step=Step.SEARCHING,
                    origin=q.origin,
                    destination=q.destination,
                    depart=q.depart,
                    adults=q.adults,
                    dialog_id=d_id,
                )
                return [SearchRequest(query=q, baggage_mode="any", dialog_id=d_id)]
            except ParseError as e:
                return [Reply(text=f"⚠️ {escape(str(e))}")]

        session = self.sessions.get(chat_id)

        if session is not None and session.step == Step.ORIGIN:
            try:
                iata = to_iata(text)
                session.origin = iata
                session.step = Step.DEST
                return [
                    Reply(
                        text="🛬 <b>Куда летим?</b>\nГород или код IATA, например <i>Ереван</i> или <i>EVN</i>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            except UnknownCityError as e:
                kb = [
                    [(sugg, f"sug:{session.dialog_id}:{self._sug_code(sugg)}")]
                    for sugg in e.suggestions
                ] + [[("✖ Отмена", f"cancel:{session.dialog_id}")]]
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\n🛫 <b>Откуда летим?</b>\nГород или код IATA, например <i>Москва</i> или <i>MOW</i>",
                        keyboard=kb,
                    )
                ]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\n🛫 <b>Откуда летим?</b>\nГород или код IATA, например <i>Москва</i> или <i>MOW</i>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.DEST:
            try:
                iata = to_iata(text)
                if iata == session.origin:
                    raise ParseError("города вылета и прилёта совпадают")
                session.destination = iata
                session.step = Step.DATE
                return [
                    Reply(
                        text="📅 <b>Когда вылет?</b>\nНапример: <code>12.12</code>, <code>12.12.2026</code> или <code>2026-12-12</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            except UnknownCityError as e:
                kb = [
                    [(sugg, f"sug:{session.dialog_id}:{self._sug_code(sugg)}")]
                    for sugg in e.suggestions
                ] + [[("✖ Отмена", f"cancel:{session.dialog_id}")]]
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\n🛬 <b>Куда летим?</b>\nГород или код IATA, например <i>Ереван</i> или <i>EVN</i>",
                        keyboard=kb,
                    )
                ]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\n🛬 <b>Куда летим?</b>\nГород или код IATA, например <i>Ереван</i> или <i>EVN</i>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.DATE:
            try:
                d = parse_date(text, today=self.today())
                session.depart = d
                session.step = Step.TRIP
                return [
                    Reply(
                        text="🔁 <b>Нужен обратный билет?</b>",
                        keyboard=[
                            [
                                ("➡️ Только туда", f"trip:{session.dialog_id}:one"),
                                ("↔️ Туда и обратно", f"trip:{session.dialog_id}:rt"),
                            ],
                            [("✖ Отмена", f"cancel:{session.dialog_id}")],
                        ],
                    )
                ]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\n📅 <b>Когда вылет?</b>\nНапример: <code>12.12</code>, <code>12.12.2026</code> или <code>2026-12-12</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.TRIP:
            return [
                Reply(
                    text="👆 Выберите вариант кнопкой\n\n🔁 <b>Нужен обратный билет?</b>",
                    keyboard=[
                        [
                            ("➡️ Только туда", f"trip:{session.dialog_id}:one"),
                            ("↔️ Туда и обратно", f"trip:{session.dialog_id}:rt"),
                        ],
                        [("✖ Отмена", f"cancel:{session.dialog_id}")],
                    ],
                )
            ]

        if session is not None and session.step == Step.RETURN_DATE:
            try:
                ret_d = parse_return_date(text, session.depart or self.today(), today=self.today())
                session.return_date = ret_d
                session.step = Step.BAGGAGE
                return [self._baggage_reply(session, edit=False)]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\n📅 <b>Когда обратно?</b>\nНапример: <code>20.12</code> или <code>20.12.2026</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.BAGGAGE:
            return [
                Reply(
                    text="👆 Выберите вариант кнопкой",
                    keyboard=self._baggage_reply(session).keyboard,
                )
            ]

        if session is not None and session.step == Step.SEARCHING:
            return [Reply(text="⏳ Поиск ещё идёт, подождите завершения.")]

        if session is not None and session.step == Step.THRESHOLD:
            cleaned = text.replace(" ", "").replace("\xa0", "").replace("₽", "").strip()
            try:
                val = int(cleaned)
            except ValueError:
                return [
                    Reply(
                        text="⚠️ Нужно число в рублях, например <code>9000</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

            if val < 0:
                return [
                    Reply(
                        text="⚠️ Нужно число в рублях, например <code>9000</code>",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

            sub_id = session.threshold_sub_id
            self.sessions[chat_id] = Session(step=Step.DONE)

            if self.subs is None or sub_id is None:
                return [
                    Reply(
                        text="Отслеживание недоступно",
                        keyboard=[[("🔔 Мои отслеживания", "menu:track")]],
                    )
                ]

            if val == 0:
                ok = self.subs.set_threshold(sub_id, int(chat_id), None)
                if not ok:
                    return [
                        Reply(
                            text="Подписка не найдена",
                            keyboard=[[("🔔 Мои отслеживания", "menu:track")]],
                        )
                    ]
                return [
                    Reply(
                        text="🎯 Порог убран",
                        keyboard=[[("🔔 Мои отслеживания", "menu:track")]],
                    )
                ]
            else:
                ok = self.subs.set_threshold(sub_id, int(chat_id), val)
                if not ok:
                    return [
                        Reply(
                            text="Подписка не найдена",
                            keyboard=[[("🔔 Мои отслеживания", "menu:track")]],
                        )
                    ]
                return [
                    Reply(
                        text=f"🎯 Порог <b>{money(val, 'RUB')}</b> сохранён",
                        keyboard=[[("🔔 Мои отслеживания", "menu:track")]],
                    )
                ]

        # Outside dialog: step MENU, DONE or no session
        snap = self.store.get(chat_id)
        is_num = text.isdigit() or (text.startswith("-") and text[1:].isdigit())

        if is_num:
            if snap is None:
                return [
                    Reply(
                        text="⌛ Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            n = int(text)
            offer = snap.offer_by_number(n)
            if offer is not None:
                if session is None or not session.dialog_id:
                    session = Session(step=Step.DONE, dialog_id=secrets.token_hex(2))
                    self.sessions[chat_id] = session
                card = format_details(offer, snap.query)
                return [
                    Reply(
                        text=card,
                        keyboard=[
                            [
                                ("⬅️ К списку", f"list:{session.dialog_id}"),
                                ("🔔 Следить за ценой", f"track:{session.dialog_id}"),
                            ],
                            [("🔎 Новый поиск", f"new:{session.dialog_id}")],
                        ],
                    )
                ]
            return [Reply(text=f"🤔 Нет варианта с таким номером. Доступно: 1–{len(snap.flat)}")]

        if snap is not None:
            return [Reply(text=f"🤔 Нет варианта с таким номером. Доступно: 1–{len(snap.flat)}")]

        return [self.start_menu(chat_id)]
