"""Telegram-бот на long polling (только httpx, без лишних зависимостей)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from html import escape
import logging
import secrets
from pathlib import Path
import shutil
import time
from typing import Any

import httpx

from . import admin_panel, advice, analytics, charts, forecast
from .backup import backup_all, list_backups
from .bg_collector import BackgroundCollector
from .config import Settings, load_settings
from .dialog import (
    HELP,
    Dialog,
    HistoryRequest,
    Reply,
    SearchRequest,
    Session,
    Step,
    compact_keyboard,
    to_reply_markup,
)
from .filters import apply_criteria
from .formatting import build_compact_report, build_report, date_ru, format_details
from .guard import friendly_error
from .history import PriceHistory
from .metrics import MetricsStore
from .models import Query
from .offers import assign_ids, filter_by_baggage
from .parsing import city_name
from .phones import PhoneRegistry
from .pipeline import search
from .prio_gate import PriorityGate
from .scheduler import Scheduler
from .store import ResultStore
from .subscriptions import SubscriptionStore
from .users import UserStore

log = logging.getLogger("avia.bot")
API = "https://api.telegram.org/bot{token}/{method}"


async def _search_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Вызывает search с priority, а при несовместимости мока (TypeError) повторяет без priority."""
    samples_dir = kwargs.pop("samples_dir", None)
    from .samples import sample_capture_context
    with sample_capture_context(samples_dir):
        try:
            return await fn(*args, **kwargs)
        except TypeError:
            kwargs.pop("priority", None)
            return await fn(*args, **kwargs)


class Bot:
    def __init__(
        self,
        settings: Settings,
        store: ResultStore | None = None,
        dialog: Dialog | None = None,
    ):
        if not settings.tg_token:
            raise SystemExit("Задайте TG_BOT_TOKEN в .env")
        if not settings.tg_allowed_ids and not settings.tg_open:
            raise SystemExit("Задайте TG_ALLOWED_IDS в .env (ваш Telegram id) или TG_OPEN=1, иначе некому отвечать")
        self.s = settings
        self.started_at: float = time.time()
        self.http = httpx.AsyncClient(timeout=70)
        self.busy: set[int] = set()
        self.users = UserStore(settings.storage_dir / "users.sqlite")
        self.phones = PhoneRegistry(
            storage_dir=settings.storage_dir,
            phones_new_policy=getattr(settings, "phones_new_policy", "check"),
            phones_snapshot_ttl_seconds=getattr(settings, "phones_snapshot_ttl_seconds", 180),
            phones_hold=getattr(settings, "phones_hold", None),
        )
        self.s.phones = self.phones
        self._phones_fail_count: int = 0
        self._phones_task: asyncio.Task | None = None
        try:
            self.metrics: MetricsStore | None = MetricsStore(settings.storage_dir / "metrics.sqlite")
        except Exception:
            log.exception("метрики недоступны")
            self.metrics = None
        self.last_backup_at: float | None = self._find_last_backup_time()
        self._last_forecast_train: float = 0.0
        self._gate = PriorityGate(settings.max_parallel_searches)
        self._active_searches: int = 0
        self.history = PriceHistory(settings.storage_dir / "history.sqlite")
        self.bg_collector: BackgroundCollector | None = None
        if self.s.bg_collect:
            self.bg_collector = BackgroundCollector(
                history=self.history,
                search_fn=self._bg_search,
                is_idle=self._is_idle,
                settings=self.s,
            )
        self._bg_task: asyncio.Task | None = None
        self._hist_times: dict[int, float] = {}
        if dialog is not None:
            self.dialog = dialog
            self.store = store if store is not None else dialog.store
            self.subs = dialog.subs
        else:
            self.subs = SubscriptionStore(settings.storage_dir / "subscriptions.sqlite")
            self.store = store if store is not None else ResultStore()
            self.dialog = Dialog(store=self.store, subs=self.subs)
        self._backup_in_progress: bool = False
        self._last_manual_backup_at: float = 0.0
        self._last_parser_watch: float = 0.0
        self._last_samples_check: float = 0.0
        try:
            from .samples import install_sample_hook
            install_sample_hook()
        except Exception:
            log.exception("ошибка установки перехватчика образцов")

    async def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        r = await self.http.post(API.format(token=self.s.tg_token, method=method), json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data}")
        return data["result"]

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        if len(text) <= 4096:
            params: dict = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                params["reply_markup"] = reply_markup
            return await self.call("sendMessage", **params)

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            if current_len + len(line) > 4096:
                if current:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
                while len(line) > 4096:
                    chunks.append(line[:4096])
                    line = line[4096:]
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))

        last_res = None
        for i, chunk in enumerate(chunks):
            params = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if i == len(chunks) - 1 and reply_markup is not None:
                params["reply_markup"] = reply_markup
            last_res = await self.call("sendMessage", **params)
        return last_res

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str | None = None,
    ):
        data: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
            data["parse_mode"] = "HTML"
        files = {"photo": ("chart.png", png_bytes, "image/png")}
        try:
            r = await self.http.post(
                API.format(token=self.s.tg_token, method="sendPhoto"),
                data=data,
                files=files,
            )
            res = r.json()
            if not res.get("ok"):
                raise RuntimeError(f"telegram sendPhoto: {res}")
            return res.get("result")
        except Exception as e:
            log.error("send_photo failed for chat %s: %s", chat_id, e)
            raise

    async def _process_actions(
        self,
        chat_id: int,
        actions: list[Reply | SearchRequest | HistoryRequest],
        message_id: int | None = None,
    ):
        for item in actions:
            if isinstance(item, Reply):
                reply_markup = to_reply_markup(item.keyboard)
                if item.edit and message_id is not None:
                    try:
                        await self.call(
                            "editMessageText",
                            chat_id=chat_id,
                            message_id=message_id,
                            text=item.text,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        await self.send(chat_id, item.text, reply_markup=reply_markup)
                else:
                    if item.text:
                        await self.send(chat_id, item.text, reply_markup=reply_markup)
            elif isinstance(item, SearchRequest):
                await self._run_search(chat_id, item)
            elif isinstance(item, HistoryRequest):
                await self._handle_history_request(chat_id, item)

    async def _handle_history_request(self, chat_id: int, req: HistoryRequest) -> None:
        try:
            q = req.query
            if self.history is None:
                await self.send(chat_id, "Пока мало данных по этому маршруту, загляните позже")
                return

            def _load_data_and_render():
                stats = self.history.route_stats(q)
                series = self.history.route_series(q)
                site_stats = self.history.cheapest_site_stats(q)

                curve = None
                try:
                    curve = self.history.price_curve()
                except Exception:
                    curve = None

                adv = None
                try:
                    dep_d = q.depart
                    if isinstance(dep_d, datetime):
                        dep_d = dep_d.date()
                    elif isinstance(dep_d, str):
                        try:
                            dep_d = date.fromisoformat(dep_d.strip())
                        except Exception:
                            dep_d = None
                    d = (dep_d - date.today()).days if dep_d is not None else None
                    adv = advice.build_advice(stats, d, curve=curve)
                    if adv is not None and self.s.forecast_enabled and self.history:
                        try:
                            prob = forecast.predict_drop_prob(self.history, q, stats, self.s.forecast_dir)
                            if prob is not None:
                                adv.model_prob = prob
                        except Exception:
                            log.exception("ошибка прогноза модели")
                except Exception:
                    adv = None

                orig = city_name(q.origin)
                dest = city_name(q.destination)

                def _fmt_date(d: Any) -> str:
                    if isinstance(d, (date, datetime)):
                        return d.strftime("%d.%m")
                    if isinstance(d, str):
                        try:
                            return date.fromisoformat(d.strip()).strftime("%d.%m")
                        except Exception:
                            return d
                    return str(d)

                d_str = _fmt_date(q.depart)
                if q.return_date:
                    r_str = _fmt_date(q.return_date)
                    date_title = f"{d_str}–{r_str}"
                else:
                    date_title = d_str
                title = f"{orig} → {dest}, {date_title}"

                median = stats["median"] if stats else None
                now_price = stats["last_price"] if stats else None

                png = charts.render_price_chart(series, median, title, now_price=now_price)
                return stats, series, site_stats, png, adv

            stats, series, site_stats, png, adv = await asyncio.to_thread(_load_data_and_render)

            lines = ["📊 <b>История цены</b>"]
            sum_lines = analytics.summary_lines(stats, site_stats)
            if sum_lines:
                lines.extend(sum_lines)

            adv_lines = advice.format_advice(adv) if adv else []
            if adv_lines:
                lines.append("")
                lines.extend(adv_lines)

            caption = "\n".join(lines)
            if len(caption) > 1024:
                if adv_lines and len(adv_lines) >= 3 and adv_lines[-1] in lines:
                    lines.remove(adv_lines[-1])
                    caption = "\n".join(lines)
                if len(caption) > 1024:
                    caption = caption[:1024]

            if png is not None:
                await self.send_photo(chat_id, png, caption=caption)
            elif stats is not None:
                await self.send(chat_id, caption)
            else:
                await self.send(chat_id, "Пока мало данных по этому маршруту, загляните позже")
        except Exception:
            log.exception("history chart failed")
            await self.send(chat_id, "Не удалось построить график")

    async def _run_search(self, chat_id: int, req: SearchRequest):
        if chat_id in self.busy:
            await self.send(chat_id, "⏳ Предыдущий поиск ещё идёт, подождите.")
            return
        if (
            chat_id not in self.s.tg_allowed_ids
            and not self.users.try_use_search(chat_id, self.s.daily_search_limit)
        ):
            await self.send(
                chat_id,
                f"😴 На сегодня лимит поисков исчерпан ({self.s.daily_search_limit}). Завтра можно снова, "
                "отслеживания продолжают работать.",
            )
            return
        self.busy.add(chat_id)
        q = req.query
        baggage_mode = req.baggage_mode
        wait_msg_id = None
        try:
            wait_msg = await self.send(
                chat_id,
                f"🔍 <b>Ищу билеты</b>\n{escape(city_name(q.origin))} → {escape(city_name(q.destination))}, {date_ru(q.depart)}\n\n"
                "<i>Опрашиваю сервисы, это займёт несколько минут…</i>",
            )
            if isinstance(wait_msg, dict):
                wait_msg_id = wait_msg.get("message_id")

            self._active_searches += 1
            t_search0 = time.monotonic()
            t_gate = t_search0
            try:
                async with self._gate.slot(0):
                    t_gate = time.monotonic()
                    results = await _search_call(
                        search,
                        q,
                        self.s,
                        priority=0,
                        samples_dir=self.s.storage_dir / "samples",
                    )
            finally:
                self._active_searches -= 1
            self._record_metrics("user", results)
            if self.metrics is not None and results:
                self.metrics.record_search(  # type: ignore[attr-defined]
                    "user", time.monotonic() - t_search0, results, queue_seconds=t_gate - t_search0
                )
            results = assign_ids(results)
            try:
                await asyncio.to_thread(self.history.annotate, q, results)
            except Exception:
                log.exception("история цен недоступна")
            filtered_crit, dropped_criteria = apply_criteria(results, req.criteria)
            filtered, dropped_baggage = filter_by_baggage(filtered_crit, baggage_mode)
            chunks, flat, pool = build_compact_report(
                q,
                filtered,
                req.criteria,
                baggage_mode=baggage_mode,
                dropped_baggage=dropped_baggage,
                dropped_criteria=dropped_criteria,
            )

            session = self.dialog.sessions.get(chat_id)
            if session is None or not session.dialog_id:
                session = Session(
                    step=Step.DONE,
                    origin=q.origin,
                    destination=q.destination,
                    depart=q.depart,
                    return_date=q.return_date,
                    adults=q.adults,
                    baggage_mode=baggage_mode,
                    criteria=req.criteria,
                    dialog_id=secrets.token_hex(2),
                )
                self.dialog.sessions[chat_id] = session
            else:
                session.step = Step.DONE

            d_id = session.dialog_id
            shown_ids = {o.offer_id for o in flat if o.offer_id}
            has_more = any(o.offer_id not in shown_ids for o in pool)
            kb = compact_keyboard(d_id, has_more=has_more)
            reply_markup = to_reply_markup(kb)

            for i, chunk in enumerate(chunks):
                rm = reply_markup if i == len(chunks) - 1 else None
                await self.send(chat_id, chunk, reply_markup=rm)

            self.store.put(
                chat_id,
                q,
                filtered,
                baggage_mode,
                flat,
                criteria=req.criteria,
                pool=pool,
                shown=len(flat),
                view="compact",
                dropped_baggage=dropped_baggage,
                dropped_criteria=dropped_criteria,
            )
        except Exception as e:
            log.exception("search failed")
            await self.send(chat_id, f"⚠️ {escape(friendly_error(e))}")
        finally:
            self.busy.discard(chat_id)
            if wait_msg_id is not None:
                try:
                    await self.call("deleteMessage", chat_id=chat_id, message_id=wait_msg_id)
                except Exception:
                    try:
                        await self.call("editMessageText", chat_id=chat_id, message_id=wait_msg_id, text="Готово")
                    except Exception:
                        pass

    def _admit(self, user: dict, chat: dict | None) -> bool:
        """Пускает администраторов и (в открытом режиме) любого человека из личного чата; регистрирует в users."""
        user_id = user.get("id")
        if not isinstance(user_id, int) or user.get("is_bot"):
            return False
        is_admin = user_id in self.s.tg_allowed_ids
        if not is_admin:
            if not self.s.tg_open:
                return False
            # подписки привязаны к tg id = id личного чата, группы и каналы не обслуживаем
            if chat is not None and chat.get("type", "private") != "private":
                return False
        try:
            rec = self.users.register(user_id, user.get("username"), user.get("first_name"))
        except Exception:
            log.exception("не удалось записать пользователя")
            return is_admin
        return is_admin or not rec.blocked

    async def handle(self, msg: dict):
        chat_id = msg["chat"]["id"]
        if not self._admit(msg.get("from", {}), msg.get("chat")):
            return
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0] if text else ""
        if cmd == "/admin":
            if msg.get("from", {}).get("id") in self.s.tg_allowed_ids:
                await self._show_admin_panel(chat_id, section="home")
            return
        if cmd == "/phone":
            if msg.get("from", {}).get("id") in self.s.tg_allowed_ids:
                await self._phone_command(chat_id, text)
            return
        if text.startswith(("/stats", "/block", "/unblock")):
            if msg.get("from", {}).get("id") in self.s.tg_allowed_ids:
                await self._admin_command(chat_id, text)
            return
        actions = self.dialog.on_text(chat_id, text)
        await self._process_actions(chat_id, actions)

    async def _admin_command(self, chat_id: int, text: str) -> None:
        """/stats — сводка и последние люди; /block <id>, /unblock <id> — закрыть или вернуть доступ."""
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0]
        if cmd == "/stats":
            st = self.users.stats()
            lines = [
                f"Людей: {st['users']} (заблокировано {st['blocked']})",
                f"Поисков сегодня: {st['today']}, всего: {st['searches']}",
                "",
                "Последние:",
            ]
            for u in self.users.recent(10):
                name = f"@{u.username}" if u.username else (u.first_name or "—")
                mark = " 🚫" if u.blocked else ""
                lines.append(f"{u.tg_id} {escape(name)} · поисков {u.total_searches}{mark}")

            # Дополнительные блоки аналитики
            # 1. Популярные маршруты (7 дней)
            try:
                if self.history is not None:
                    routes = self.history.popular_routes(5, 7)
                    lines.append("")
                    lines.append("Популярные маршруты (7 дней):")
                    if routes:
                        for orig, dest, count in routes:
                            lines.append(f"{escape(city_name(orig))} → {escape(city_name(dest))}: {count}")
                    else:
                        lines.append("—")
            except Exception:
                log.exception("stats: popular_routes failed")

            # 2. Уведомления по причинам (7 дней)
            try:
                if self.subs is not None:
                    since_ts = time.time() - 7 * 86400.0
                    reasons_dict = self.subs.notification_reason_counts(since_ts)
                    lines.append("")
                    lines.append("Уведомления по причинам (7 дней):")
                    if reasons_dict:
                        for r_name, r_cnt in reasons_dict.items():
                            lines.append(f"{escape(r_name)}: {r_cnt}")
                    else:
                        lines.append("—")
            except Exception:
                log.exception("stats: notification_reason_counts failed")

            # 3. Очередь проверок
            try:
                if self.subs is not None:
                    now_ts = time.time()
                    today_d = (
                        self.dialog.today()
                        if hasattr(self, "dialog") and hasattr(self.dialog, "today") and callable(self.dialog.today)
                        else date.today()
                    )
                    ov = self.subs.overdue_stats(now_ts, today_d)
                    lines.append("")
                    lines.append("Очередь проверок:")
                    lines.append(
                        f"Подписок {ov.get('total', 0)}, просрочено {ov.get('overdue', 0)}, максимум отстаёт на {ov.get('max_lag_minutes', 0)} мин"
                    )
            except Exception:
                log.exception("stats: overdue_stats failed")

            # 4. Мак работал сегодня
            try:
                if self.history is not None:
                    now_ts = time.time()
                    now_dt = datetime.fromtimestamp(now_ts)
                    start_of_day = datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()
                    cov = self.history.coverage(start_of_day, now_ts)
                    covered_s = cov.get("covered_seconds", 0.0)
                    gaps = cov.get("gaps", [])
                    worked_h = int(round(covered_s / 3600.0))
                    total_h = int(round(max(0.0, now_ts - start_of_day) / 3600.0))
                    lines.append("")
                    lines.append("Мак работал сегодня:")
                    lines.append(f"Работал {worked_h} ч из {total_h}, пропусков {len(gaps)}")
            except Exception:
                log.exception("stats: coverage failed")

            # 5. Фоновый сбор цен
            try:
                lines.append("")
                if self.s.bg_collect:
                    n = self.bg_collector.done_today if self.bg_collector is not None else 0
                    m = self.s.bg_collect_max_per_day
                    lines.append(f"Фоновый сбор: сегодня {n} из {m}")
                else:
                    lines.append("Фоновый сбор: выключен")
            except Exception:
                log.exception("stats: bg_collect failed")

            lines.append("")
            lines.append("Подробнее: /admin")

            await self.send(chat_id, "\n".join(lines))
            return
        arg = arg.strip()
        if not arg.lstrip("-").isdigit():
            await self.send(chat_id, f"Формат: {cmd} <tg id>")
            return
        target = int(arg)
        if target in self.s.tg_allowed_ids:
            await self.send(chat_id, "Администратора заблокировать нельзя.")
            return
        ok = self.users.set_blocked(target, cmd == "/block")
        if not ok:
            await self.send(chat_id, "Такого пользователя нет в базе.")
        else:
            await self.send(chat_id, f"{target}: {'заблокирован' if cmd == '/block' else 'разблокирован'}")

    async def _phone_command(self, chat_id: int, text: str) -> None:
        """Обрабатывает /phone <id> test | allow site1,site2 | deny site1 | all | hold | on."""
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 3:
            await self.send(
                chat_id,
                "Формат: <code>/phone &lt;id&gt; test | allow s1,s2 | deny s1 | all | hold | on</code>",
            )
            return

        phone_id = parts[1].strip()
        subcmd_raw = parts[2].strip()
        subparts = subcmd_raw.split(maxsplit=1)
        subcmd = subparts[0].lower()
        subargs = subparts[1].strip() if len(subparts) > 1 else ""

        all_phones = self.phones.all()
        if phone_id not in all_phones:
            await self.send(chat_id, f"⚠️ Телефон <code>{escape(phone_id)}</code> не найден в реестре.")
            return

        valid_sites = {s.name for s in self.s.sites}

        if subcmd == "test":
            from .phone_check import is_checking, run_phone_check
            if is_checking(phone_id):
                await self.send(chat_id, f"⏳ Телефон <code>{escape(phone_id)}</code> уже проверяется.")
            else:
                await self.send(chat_id, f"🔍 Запуск проверки телефона <code>{escape(phone_id)}</code>…")
                asyncio.create_task(run_phone_check(self.s, phone_id, bot=self, initiated_by_chat_id=chat_id))
            return

        if subcmd == "hold":
            self.phones.set_status(phone_id, "hold")
            await self.send(chat_id, f"❄️ Телефон <code>{escape(phone_id)}</code>: статус изменён на <b>hold</b> (заморожен).")
            return

        if subcmd in ("on", "active"):
            self.phones.set_status(phone_id, "active")
            await self.send(chat_id, f"✅ Телефон <code>{escape(phone_id)}</code>: статус изменён на <b>active</b> (в работе).")
            return

        if subcmd == "all":
            self.phones.set_sites(phone_id, allow=None, deny=[])
            await self.send(chat_id, f"🌐 Телефон <code>{escape(phone_id)}</code>: разрешены <b>все сайты</b>.")
            return

        if subcmd == "allow":
            if not subargs:
                await self.send(chat_id, "Укажите сайты: <code>/phone &lt;id&gt; allow site1,site2</code>")
                return
            sites = [s.strip() for s in subargs.split(",") if s.strip()]
            invalid = [s for s in sites if s not in valid_sites]
            if invalid:
                avail = ", ".join(sorted(valid_sites))
                await self.send(
                    chat_id,
                    f"⚠️ Неизвестные сайты: <code>{escape(', '.join(invalid))}</code>\nДоступны: {escape(avail)}",
                )
                return
            self.phones.set_sites(phone_id, allow=sites, deny=[])
            await self.send(
                chat_id,
                f"✅ Телефон <code>{escape(phone_id)}</code>: разрешены сайты: <b>{escape(', '.join(sites))}</b>",
            )
            return

        if subcmd == "deny":
            if not subargs:
                await self.send(chat_id, "Укажите сайты: <code>/phone &lt;id&gt; deny site1,site2</code>")
                return
            sites = [s.strip() for s in subargs.split(",") if s.strip()]
            invalid = [s for s in sites if s not in valid_sites]
            if invalid:
                avail = ", ".join(sorted(valid_sites))
                await self.send(
                    chat_id,
                    f"⚠️ Неизвестные сайты: <code>{escape(', '.join(invalid))}</code>\nДоступны: {escape(avail)}",
                )
                return
            self.phones.set_sites(phone_id, allow=None, deny=sites)
            await self.send(
                chat_id,
                f"🚫 Телефон <code>{escape(phone_id)}</code>: запрещены сайты: <b>{escape(', '.join(sites))}</b>",
            )
            return

        await self.send(
            chat_id,
            "Неизвестное действие. Формат: <code>/phone &lt;id&gt; test | allow s1,s2 | deny s1 | all | hold | on</code>",
        )

    async def _handle_admin_callback(self, cq: dict, data: str) -> None:
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        user_id = cq.get("from", {}).get("id")
        if chat_id is None:
            chat_id = user_id
        message_id = msg.get("message_id")

        parts = data.split(":")
        section = parts[1] if len(parts) > 1 else "home"
        param = parts[2] if len(parts) > 2 else None

        if section == "backup_now":
            if self._backup_in_progress:
                if cq_id:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id, text="⏳ Бэкап уже выполняется…")
                return
            now = time.time()
            if now - self._last_manual_backup_at < 30.0:
                rem = max(1, int(round(30.0 - (now - self._last_manual_backup_at))))
                if cq_id:
                    await self.call(
                        "answerCallbackQuery",
                        callback_query_id=cq_id,
                        text=f"Подождите {rem} с перед следующим бэкапом",
                    )
                return
            self._backup_in_progress = True
            try:
                created = await asyncio.to_thread(self.backup_now)
                self._last_manual_backup_at = time.time()
                if cq_id:
                    await self.call(
                        "answerCallbackQuery",
                        callback_query_id=cq_id,
                        text=f"Готово: создано {len(created)} файлов",
                    )
            except Exception:
                log.exception("backup_now failed")
                if cq_id:
                    await self.call(
                        "answerCallbackQuery",
                        callback_query_id=cq_id,
                        text="⚠️ Ошибка бэкапа",
                    )
            finally:
                self._backup_in_progress = False

            await self._show_admin_panel(chat_id, section="backup", message_id=message_id)
            return

        if section == "ph":
            # Callback-запросы раздела Телефоны: adm:ph или adm:ph:hold:<id> или adm:ph:on:<id> или adm:ph:test:<id>
            if len(parts) >= 4:
                action = parts[2]
                phone_id = ":".join(parts[3:])
                if action == "hold":
                    await asyncio.to_thread(self.phones.set_status, phone_id, "hold")
                    if cq_id:
                        await self.call("answerCallbackQuery", callback_query_id=cq_id, text=f"Заморожен: {phone_id}")
                elif action in ("on", "active"):
                    await asyncio.to_thread(self.phones.set_status, phone_id, "active")
                    if cq_id:
                        await self.call("answerCallbackQuery", callback_query_id=cq_id, text=f"В работе: {phone_id}")
                elif action in ("test", "re"):
                    from .phone_check import PASSES_PER_SITE, is_checking, run_phone_check
                    if is_checking(phone_id):
                        if cq_id:
                            await self.call("answerCallbackQuery", callback_query_id=cq_id, text=f"⏳ Уже проверяется: {phone_id}")
                    else:
                        if cq_id:
                            await self.call("answerCallbackQuery", callback_query_id=cq_id, text=f"🔍 Запуск проверки: {phone_id}")
                        asyncio.create_task(
                            run_phone_check(
                                self.s, phone_id, bot=self, initiated_by_chat_id=chat_id,
                                per_site=1 if action == "re" else PASSES_PER_SITE,
                            )
                        )
            elif cq_id:
                try:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id)
                except Exception:
                    pass
            await self._show_admin_panel(chat_id, section="ph", message_id=message_id)
            return

        if cq_id:
            try:
                await self.call("answerCallbackQuery", callback_query_id=cq_id)
            except Exception:
                pass

        await self._show_admin_panel(chat_id, section=section, param=param, message_id=message_id)

    async def _show_admin_panel(
        self,
        chat_id: int,
        section: str = "home",
        param: str | None = None,
        message_id: int | None = None,
    ) -> None:
        text, reply_markup = await self._render_admin_panel(section, param)
        if message_id is not None:
            try:
                await self.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
            except Exception as e:
                err_str = str(e).lower()
                if "not modified" in err_str:
                    pass
                else:
                    log.warning("admin editMessageText failed: %s", e)
        else:
            await self.send(chat_id, text, reply_markup=reply_markup)

    async def _render_admin_panel(
        self,
        section: str,
        param: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if section == "home":
            return admin_panel.build_home(), admin_panel.make_home_keyboard()

        period = param or "24h"
        keyboard = admin_panel.make_section_keyboard(section, period=period)
        try:
            if section == "over":
                ctx = await asyncio.to_thread(self._collect_overview_data)
                text = admin_panel.build_overview(ctx)
            elif section == "parse":
                ctx = await asyncio.to_thread(self._collect_parse_data, period)
                text = admin_panel.build_parse(ctx, period=period)
            elif section == "health":
                from .health import build_health_text, collect_health
                if self.metrics is None:
                    text = "🩺 <b>Здоровье сайтов</b>\n\nМетрики не включены"
                else:
                    health = await asyncio.to_thread(collect_health, self.metrics, time.time() - 86400)
                    names = [x.name for x in self.s.sites if x.enabled and x.source == "browser"]
                    text = build_health_text(health, names)
            elif section == "ord":
                text = admin_panel.build_phone_order({"settings": self.s})
            elif section == "users":
                ctx = await asyncio.to_thread(self._collect_users_data)
                text = admin_panel.build_users(ctx)
            elif section == "db":
                ctx = await asyncio.to_thread(self._collect_db_data)
                text = admin_panel.build_db(ctx)
            elif section == "tickets":
                ctx = await asyncio.to_thread(self._collect_tickets_data)
                text = admin_panel.build_tickets(ctx)
            elif section == "subs":
                ctx = await asyncio.to_thread(self._collect_subs_data)
                text = admin_panel.build_subs(ctx)
            elif section == "backup":
                ctx = await asyncio.to_thread(self._collect_backup_data)
                text = admin_panel.build_backups(ctx)
            elif section == "ph":
                ctx = await asyncio.to_thread(self._collect_phones_data)
                text = admin_panel.build_phones(ctx)
                keyboard = admin_panel.make_phones_keyboard(ctx.get("phones", []))
            elif section == "forecast":
                ctx = await asyncio.to_thread(self._collect_forecast_data)
                text = admin_panel.build_forecast(ctx)
            else:
                text = admin_panel.build_home()
                keyboard = admin_panel.make_home_keyboard()
        except Exception:
            log.exception("admin section %s error", section)
            headers = {
                "over": "📊 <b>Обзор</b>",
                "parse": "🌐 <b>Парсинг</b>",
                "ord": "🧭 <b>Порядок телефонов</b>",
                "health": "🩺 <b>Здоровье сайтов</b>",
                "users": "👥 <b>Пользователи</b>",
                "db": "🗄 <b>База</b>",
                "tickets": "🎫 <b>Билеты</b>",
                "subs": "🔔 <b>Подписки</b>",
                "backup": "🛟 <b>Бэкапы</b>",
                "ph": "📱 <b>Телефоны</b>",
                "forecast": "🔮 <b>Прогноз</b>",
            }
            title = headers.get(section, "🛠 <b>Панель разработчика</b>")
            text = f"{title}\n\n⚠️ данные недоступны"

        return text, keyboard

    def _collect_phones_data(self) -> dict[str, Any]:
        if not hasattr(self, "phones") or self.phones is None:
            return {"phones": []}
        all_p = self.phones.all()

        def sort_key(item: tuple[str, dict[str, Any]]) -> Any:
            fs = item[1].get("first_seen")
            if isinstance(fs, (int, float)):
                return (0, float(fs))
            return (1, str(fs or ""))

        sorted_p = [v for _, v in sorted(all_p.items(), key=sort_key)]
        return {"phones": sorted_p}

    def _collect_overview_data(self) -> dict[str, Any]:
        now = time.time()
        uptime = now - self.started_at
        git_ver = admin_panel.get_git_commit(Path(__file__).resolve().parents[2])
        u_tot = self.users.count() if self.users else 0
        u_24 = self.users.count_active_since(now - 86400) if self.users else 0
        u_7d = self.users.count_active_since(now - 7 * 86400) if self.users else 0
        u_new = self.users.count_since_created(now - 86400) if self.users else 0
        st = self.users.stats() if self.users else {}
        s_user = st.get("today", 0)

        bg_pairs = None
        bg_routes = None
        if self.s.bg_collect:
            done = self.bg_collector.done_today if self.bg_collector else 0
            bg_info = f"{done} из {self.s.bg_collect_max_per_day}"
            bg_pairs = self.bg_collector.active_pairs_count if self.bg_collector else 0
            bg_routes = self.bg_collector.distinct_routes_count if self.bg_collector else 0
        else:
            bg_info = "выключен"

        active_subs = 0
        if self.subs:
            s_stats = self.subs.table_stats()
            active_subs = s_stats.get("active_subscriptions", 0)

        busy_cnt = len(self.busy)
        last_b = self.last_backup_at

        gate_info = None
        if hasattr(self, "_gate") and self._gate is not None:
            gate_info = {
                "active": self._gate.active(),
                "capacity": self._gate.capacity,
                "waiting": self._gate.waiting_by_priority(),
            }

        state_mb = 0.0
        try:
            if self.s.storage_dir.exists():
                size_b = sum(f.stat().st_size for f in self.s.storage_dir.rglob("*") if f.is_file())
                state_mb = size_b / (1024 * 1024)
        except Exception:
            pass

        return {
            "uptime_seconds": uptime,
            "git_version": git_ver,
            "users_total": u_tot,
            "users_active_24h": u_24,
            "users_active_7d": u_7d,
            "users_new_24h": u_new,
            "searches_today": s_user,
            "bg_searches": bg_info,
            "bg_active_pairs": bg_pairs,
            "bg_routes": bg_routes,
            "active_subs": active_subs,
            "busy_searches": busy_cnt,
            "last_backup_at": last_b,
            "now": now,
            "state_size_mb": state_mb,
            "search_queue": gate_info,
            "gate": self._gate,
        }

    def _collect_parse_data(self, period: str = "24h") -> dict[str, Any]:
        if self.metrics is None:
            return {
                "summary": None,
                "counts_by_source": {},
                "recent_errors": [],
                "devices_summary": {},
                "site_devices_summary": {},
            }
        now = time.time()
        since_ts = now - (7 * 86400.0 if period == "7d" else 86400.0)
        summary = self.metrics.summary(since_ts)
        counts = self.metrics.counts_by_source(since_ts)
        errors = self.metrics.recent_errors(limit=5, since_ts=since_ts)
        devices_summary = self.metrics.summary_by_device(since_ts)
        site_devices_summary = self.metrics.summary_by_site_device(since_ts)
        parser_statuses = {}
        try:
            from .parser_watch import get_parser_statuses
            parser_statuses = get_parser_statuses(self.metrics, self.s, now=now)
        except Exception:
            log.exception("ошибка сбора parser_statuses")
        return {
            "summary": summary,
            "counts_by_source": counts,
            "recent_errors": errors,
            "devices_summary": devices_summary,
            "site_devices_summary": site_devices_summary,
            "parser_statuses": parser_statuses,
        }

    def _collect_users_data(self) -> dict[str, Any]:
        now = time.time()
        st = self.users.stats() if self.users else {}
        u_tot = st.get("users", 0)
        blocked = st.get("blocked", 0)
        searches = st.get("searches", 0)
        new_24 = self.users.count_since_created(now - 86400) if self.users else 0
        new_7d = self.users.count_since_created(now - 7 * 86400) if self.users else 0
        act_24 = self.users.count_active_since(now - 86400) if self.users else 0
        act_7d = self.users.count_active_since(now - 7 * 86400) if self.users else 0
        top = self.users.top_searchers(limit=5) if self.users else []
        recent = self.users.recent(limit=5) if self.users else []
        return {
            "total": u_tot,
            "blocked": blocked,
            "searches": searches,
            "new_24h": new_24,
            "new_7d": new_7d,
            "active_24h": act_24,
            "active_7d": act_7d,
            "top_searchers": top,
            "recent_users": recent,
        }

    def _collect_db_data(self) -> dict[str, Any]:
        db_files = []
        if self.s.storage_dir.exists():
            for p in sorted(self.s.storage_dir.glob("*.sqlite")):
                if p.is_file():
                    db_files.append({"name": p.name, "size_mb": p.stat().st_size / (1024 * 1024)})

        h_stats = self.history.table_stats() if self.history else {}
        s_stats = self.subs.table_stats() if self.subs else {}
        u_cnt = self.users.count() if self.users else 0

        m_cnt = 0
        if self.metrics:
            try:
                with self.metrics._connect() as db:
                    row = db.execute("SELECT COUNT(*) FROM site_runs").fetchone()
                    m_cnt = int(row[0]) if row else 0
            except Exception:
                m_cnt = 0

        now = time.time()
        growth_24 = self.history.rows_since(now - 86400) if self.history else 0

        ph_path = self.s.storage_dir / "history.sqlite"
        total_ph = h_stats.get("price_history", 0)
        growth_mb = 0.0
        if total_ph > 0 and ph_path.exists():
            try:
                avg_row = ph_path.stat().st_size / total_ph
                growth_mb = (avg_row * growth_24) / (1024 * 1024)
            except Exception:
                growth_mb = 0.0

        disk_free_gb = 0.0
        try:
            _, _, free = shutil.disk_usage(self.s.storage_dir)
            disk_free_gb = free / (1024 ** 3)
        except Exception:
            pass

        return {
            "db_files": db_files,
            "history_stats": h_stats,
            "subscriptions_stats": s_stats,
            "users_count": u_cnt,
            "metrics_runs_count": m_cnt,
            "history_growth_24h": growth_24,
            "growth_mb_day": growth_mb,
            "disk_free_gb": disk_free_gb,
        }

    def _collect_tickets_data(self) -> dict[str, Any]:
        now = time.time()
        t_overview = self.history.tickets_overview(days=7, now=now) if self.history else {}
        pop_routes = self.history.popular_routes(limit=5, days=7, now=now) if self.history else []
        routes_list = []
        for orig, dest, count in pop_routes:
            min_p = self.history.route_min_price(orig, dest, days=7, now=now)
            routes_list.append({"origin": orig, "destination": dest, "count": count, "min_price": min_p})

        sites = self.history.site_share(days=7, now=now) if self.history else []
        fc = self._collect_forecast_data().get("forecast")

        bg_pairs = None
        bg_routes = None
        bg_done = None
        if self.s.bg_collect:
            done = self.bg_collector.done_today if self.bg_collector else 0
            bg_done = f"{done} из {self.s.bg_collect_max_per_day}"
            bg_pairs = self.bg_collector.active_pairs_count if self.bg_collector else 0
            bg_routes = self.bg_collector.distinct_routes_count if self.bg_collector else 0

        return {
            "routes_count": t_overview.get("routes_count", 0),
            "searches_count": t_overview.get("searches_count", 0),
            "avg_days_to_departure": t_overview.get("avg_days_to_departure"),
            "popular_routes": routes_list,
            "baggage": {
                "with": t_overview.get("baggage_with", 0),
                "without": t_overview.get("baggage_without", 0),
                "unknown": t_overview.get("baggage_unknown", 0),
                "total": t_overview.get("baggage_total", 0),
            },
            "site_share": sites,
            "forecast": fc,
            "bg_active_pairs": bg_pairs,
            "bg_routes": bg_routes,
            "bg_done": bg_done,
        }

    def _collect_forecast_data(self) -> dict[str, Any]:
        fc = None
        if self.s.forecast_enabled:
            try:
                fc = forecast.load_report(self.s.forecast_dir)
                if fc is None and self.history:
                    departed = self.history.count_departed_flights()
                    fc = {
                        "trained_at": None,
                        "train_rows": 0,
                        "test_rows": 0,
                        "routes_count": 0,
                        "departed_flights_count": departed,
                        "verdict": "рано: нужно ещё 500 ответов",
                        "metrics": None,
                    }
            except Exception:
                fc = None
        return {"forecast": fc}

    def _collect_subs_data(self) -> dict[str, Any]:
        now = time.time()
        today_d = (
            self.dialog.today()
            if hasattr(self, "dialog") and hasattr(self.dialog, "today") and callable(self.dialog.today)
            else date.today()
        )
        r_stats = self.subs.route_stats(limit=5) if self.subs else {}
        ov = self.subs.overdue_stats(now, today_d) if self.subs else {}
        reasons = self.subs.notification_reason_counts(now - 7 * 86400) if self.subs else {}
        n_24 = self.subs.notifications_since(now - 86400) if self.subs else 0

        return {
            "active_count": r_stats.get("active_count", ov.get("active", 0)),
            "groups_count": r_stats.get("groups_count", 0),
            "top_routes": r_stats.get("top_routes", []),
            "overdue": ov,
            "reasons": reasons,
            "notifications_24h": n_24,
        }

    def _collect_backup_data(self) -> dict[str, Any]:
        backups = list_backups(self.s.backup_dir)
        return {
            "keep": self.s.backup_keep,
            "hour": self.s.backup_hour,
            "backups": backups,
        }

    async def handle_callback(self, cq: dict):
        user_id = cq.get("from", {}).get("id")
        data = cq.get("data", "")
        cq_id = cq.get("id")

        if data.startswith("adm:"):
            if user_id not in self.s.tg_allowed_ids:
                if cq_id:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id, text="Нет доступа")
                return
            await self._handle_admin_callback(cq, data)
            return

        msg = cq.get("message") or {}
        if not self._admit(cq.get("from", {}), msg.get("chat")):
            return
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            chat_id = user_id
        message_id = msg.get("message_id")

        if data.startswith("hist:"):
            now = time.time()
            if now - self._hist_times.get(chat_id, 0.0) < 5.0:
                await self.call("answerCallbackQuery", callback_query_id=cq_id, text="Секунду…")
                return
            self._hist_times[chat_id] = now

        answered = False
        try:
            actions = self.dialog.on_callback(chat_id, data)
            answer_text = None
            for item in actions:
                if isinstance(item, Reply) and item.answer:
                    answer_text = item.answer
                    break

            if answer_text:
                await self.call("answerCallbackQuery", callback_query_id=cq_id, text=answer_text)
            else:
                await self.call("answerCallbackQuery", callback_query_id=cq_id)
            answered = True

            await self._process_actions(chat_id, actions, message_id=message_id)
        finally:
            if not answered:
                try:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id)
                except Exception:
                    pass

    def _record_metrics(self, source: str, results: Any) -> None:
        if self.metrics is None or not results:
            return
        try:
            self.metrics.record_results(source, results)
        except Exception:
            log.exception("ошибка записи метрик")

    def _find_last_backup_time(self) -> float | None:
        try:
            if not self.s.backup_dir.exists():
                return None
            files = [p for p in self.s.backup_dir.glob("*.sqlite.gz") if p.is_file()]
            if not files:
                return None
            return max(p.stat().st_mtime for p in files)
        except Exception:
            return None

    def _should_run_daily_backup(self, now: datetime | None = None) -> bool:
        dt = now if now is not None else datetime.now()
        if dt.hour < self.s.backup_hour:
            return False
        today_cutoff = dt.replace(hour=self.s.backup_hour, minute=0, second=0, microsecond=0).timestamp()
        last_time = self._find_last_backup_time()
        if last_time is not None and last_time >= today_cutoff:
            return False
        return True

    def backup_now(self) -> list[Path]:
        """Создаёт бэкап всех баз SQLite сейчас."""
        files = backup_all(self.s.storage_dir, self.s.backup_dir, self.s.backup_keep)
        self.last_backup_at = self._find_last_backup_time()
        return files

    def _is_idle(self) -> bool:
        return not self.busy and self._active_searches == 0

    async def _bg_search(self, q: Query):
        async with self._gate.slot(2):
            results = await _search_call(
                search,
                q,
                self.s,
                priority=2,
                samples_dir=self.s.storage_dir / "samples",
            )
        self._record_metrics("bg", results)
        return results

    async def _scheduler_search(self, q):
        # телефоны общие: фоновый опрос стоит в той же очереди, что и поиски людей
        self._active_searches += 1
        try:
            async with self._gate.slot(1):
                results = await _search_call(
                    search,
                    q,
                    self.s,
                    priority=1,
                    samples_dir=self.s.storage_dir / "samples",
                )
        finally:
            self._active_searches -= 1
        self._record_metrics("sub", results)
        return results

    async def _scheduler_notify(self, chat_id: int, text: str, reply_markup: dict | None) -> None:
        await self.send(chat_id, text, reply_markup=reply_markup)

    async def _notify_admins(self, text: str, reply_markup: dict | None = None) -> None:
        """Отправляет сообщение всем администраторам из tg_allowed_ids."""
        if not self.s.tg_allowed_ids:
            return
        for admin_id in self.s.tg_allowed_ids:
            try:
                await self.send(admin_id, text, reply_markup=reply_markup)
            except Exception:
                log.exception("ошибка отправки сообщения администратору %s", admin_id)

    async def _fetch_hub_devices(self) -> list[dict[str, Any]]:
        """Запрашивает список устройств из хаба GET /api/devices."""
        url = self.s.droidhub_url.strip().rstrip("/") + "/api/devices"
        headers = {}
        if self.s.droidhub_token:
            headers["Authorization"] = f"Bearer {self.s.droidhub_token.strip()}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("Ответ /api/devices не является списком")
            return data

    async def _check_hub_devices_once(self) -> list[Any]:
        """Один шаг опроса хаба и обновления реестра. Не падает при ошибках."""
        try:
            devices = await self._fetch_hub_devices()
            self._phones_fail_count = 0
            if not isinstance(devices, list):
                return []
            new_devs = await asyncio.to_thread(self.phones.refresh, devices)
            for dev in new_devs:
                await self._notify_new_phone(dev)
            return new_devs
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._phones_fail_count += 1
            if self._phones_fail_count == 1 or self._phones_fail_count % 5 == 0:
                log.warning("Ошибка опроса хаба телефонов (попытка %d): %s", self._phones_fail_count, e)
            return []

    async def _phones_refresh_loop(self) -> None:
        """Периодическое автообнаружение телефонов из хаба."""
        interval = max(5.0, float(getattr(self.s, "phones_refresh_seconds", 60)))
        while True:
            try:
                await self._check_hub_devices_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def _notify_new_phone(self, dev: Any) -> None:
        """Оповещает администраторов о новом телефоне в хабе."""
        dev_id = getattr(dev, "id", None) or (dev.get("id") if isinstance(dev, dict) else str(dev))
        name = getattr(dev, "name", None) or (dev.get("name") if isinstance(dev, dict) else dev_id)
        status = getattr(dev, "status", None) or (dev.get("status") if isinstance(dev, dict) else "active")

        safe_name = escape(str(name))
        safe_id = escape(str(dev_id))

        if status == "new":
            p_info = self.phones.get(dev_id) or {} if hasattr(self, "phones") and self.phones else {}
            batt = p_info.get("battery")
            charging = p_info.get("charging", False)
            ch_str = " ⚡" if charging else ""
            batt_str = f"{batt}%{ch_str}" if batt is not None else "—"
            msg = (
                f"📱 Обнаружен телефон {safe_name} ({safe_id}, батарея {batt_str}); "
                f"откройте /admin → 📱 Телефоны и нажмите «🔍 Проверить»"
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": f"🔍 Проверить {safe_name}", "callback_data": f"adm:ph:test:{safe_id}"},
                        {"text": "📱 Телефоны", "callback_data": "adm:ph"},
                    ]
                ]
            }
        else:
            status_text = "взят в работу" if status == "active" else "заморожен (ожидает одобрения)"
            msg = (
                f"📱 Новый телефон в хабе: {safe_name} ({safe_id}). "
                f"Статус: {status_text}…\n\n"
                f"Управление: /admin → «Телефоны» (кнопки в панели) или команда <code>/phone {safe_id} hold|on|all|allow|deny</code>"
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "📱 Телефоны в панели", "callback_data": "adm:ph"},
                    ]
                ]
            }
        await self._notify_admins(msg, reply_markup=reply_markup)

    async def _maintenance_loop(self, tick_seconds: float = 300.0) -> None:
        """Отмечает, что бот жив (для учёта пропусков), и раз в сутки сворачивает и чистит историю цен."""
        last_prune = 0.0
        while True:
            try:
                await asyncio.to_thread(self.history.record_heartbeat)
                if time.time() - last_prune >= 86400:
                    await asyncio.to_thread(self.history.rebuild_route_daily)
                    await asyncio.to_thread(self.history.prune)
                    if self.metrics is not None:
                        await asyncio.to_thread(self.metrics.prune)
                    last_prune = time.time()

                if self._should_run_daily_backup():
                    try:
                        await asyncio.to_thread(self.backup_now)
                    except Exception:
                        log.exception("ошибка ежедневного бэкапа")

                if self.s.forecast_enabled and self.history:
                    if self._should_run_forecast_training():
                        try:
                            await asyncio.to_thread(
                                forecast.train_and_eval,
                                self.history,
                                self.s.forecast_dir,
                            )
                            self._last_forecast_train = time.time()
                        except Exception:
                            log.exception("ошибка обучения модели прогноза")

                if self.s.parser_watch_enabled and self.metrics is not None:
                    if time.time() - getattr(self, "_last_health_check", 0.0) >= 300:
                        self._last_health_check = time.time()
                        try:
                            from .health import check_health_alerts
                            for alert_text in await asyncio.to_thread(
                                check_health_alerts, self.metrics, None, float(self.s.parser_watch_cooldown_hours)
                            ):
                                await self._notify_admins(alert_text)
                        except Exception:
                            log.exception("ошибка проверки здоровья сайтов")
                    if time.time() - self._last_parser_watch >= 1800:
                        try:
                            from .parser_watch import check_parser_watch
                            alerts = await asyncio.to_thread(
                                check_parser_watch,
                                self.metrics,
                                self.s,
                            )
                            for alert_text in alerts:
                                await self._notify_admins(alert_text)
                            self._last_parser_watch = time.time()
                        except Exception:
                            log.exception("ошибка проверки парсеров в parser_watch")

                if time.time() - self._last_samples_check >= 7 * 86400:
                    try:
                        samples_dir = self.s.storage_dir / "samples"
                        if samples_dir.exists():
                            from .samples import check_samples_with_cooldown
                            failures = await asyncio.to_thread(
                                check_samples_with_cooldown,
                                samples_dir,
                                self.metrics,
                                cooldown_hours=float(self.s.parser_watch_cooldown_hours),
                            )
                            for fail_msg in failures:
                                await self._notify_admins(fail_msg)
                        self._last_samples_check = time.time()
                    except Exception:
                        log.exception("ошибка еженедельной проверки образцов")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("обслуживание истории цен")
            await asyncio.sleep(tick_seconds)

    def _should_run_forecast_training(self) -> bool:
        if not self.s.forecast_enabled or not self.history:
            return False
        retrain_seconds = self.s.forecast_retrain_days * 86400.0
        now = time.time()
        if self._last_forecast_train > 0:
            return (now - self._last_forecast_train) >= retrain_seconds

        report_path = self.s.forecast_dir / "report.json"
        if not report_path.exists():
            return True
        try:
            mtime = report_path.stat().st_mtime
            if (now - mtime) >= retrain_seconds:
                return True
            self._last_forecast_train = mtime
            return False
        except Exception:
            return True

    async def run(self):
        offset = 0
        log.info("bot started")
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        if self.s.droidhub_url and self.s.droidhub_token:
            self._phones_task = asyncio.create_task(self._phones_refresh_loop())
        if self.subs is not None:
            scheduler = Scheduler(
                self.subs,
                self._scheduler_search,
                self._scheduler_notify,
                history=self.history,
            )
            self._scheduler_task = asyncio.create_task(scheduler.run_forever())
        if self.s.bg_collect:
            if self.bg_collector is None:
                self.bg_collector = BackgroundCollector(
                    history=self.history,
                    search_fn=self._bg_search,
                    is_idle=self._is_idle,
                    settings=self.s,
                )
            self._bg_task = asyncio.create_task(self.bg_collector.run_forever())
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception:
                log.exception("getUpdates")
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    asyncio.create_task(self.handle(u["message"]))
                elif "callback_query" in u:
                    asyncio.create_task(self.handle_callback(u["callback_query"]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx на уровне INFO пишет полный URL запроса, а в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(Bot(load_settings()).run())


if __name__ == "__main__":
    main()
