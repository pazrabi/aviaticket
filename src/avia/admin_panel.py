"""Панель разработчика: формирование текстов и inline-клавиатур."""
from __future__ import annotations

from datetime import date, datetime
from html import escape
import logging
from pathlib import Path
import subprocess
import time
from typing import Any

from .parsing import city_name

log = logging.getLogger("avia.admin_panel")


def get_git_commit(cwd: Path | str | None = None) -> str:
    """Возвращает короткий хэш git-коммита проекта (2 с таймаут) или '—'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=cwd or Path(__file__).resolve().parents[2],
        )
        if res.returncode == 0:
            val = res.stdout.strip()
            return val if val else "—"
        return "—"
    except Exception:
        return "—"


def format_uptime(seconds: float) -> str:
    """Форматирует аптайм вида '2 дн 3 ч' или '4 ч 15 мин'."""
    s = max(0, int(seconds))
    days = s // 86400
    hours = (s % 86400) // 3600
    minutes = (s % 3600) // 60
    if days > 0:
        return f"{days} дн {hours} ч"
    if hours > 0:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def format_backup_time(ts: float | None, now: float | None = None) -> str:
    """Форматирует время бэкапа и 'N ч назад'."""
    if ts is None:
        return "—"
    now_ts = time.time() if now is None else float(now)
    diff = max(0.0, now_ts - ts)
    dt_str = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
    hours = int(diff // 3600)
    if hours >= 24:
        days = hours // 24
        return f"{dt_str} ({days} дн назад)"
    if hours > 0:
        return f"{dt_str} ({hours} ч назад)"
    mins = max(1, int(diff // 60))
    return f"{dt_str} ({mins} мин назад)"


def truncate_text(text: str, max_chars: int = 3800) -> str:
    """Укорачивает текст до max_chars с сохранением читаемости."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_nl = cut.rfind("\n")
    if last_nl > max_chars // 2:
        cut = cut[:last_nl]
    return cut + "\n\n… [часть данных обрезана]"


def make_home_keyboard() -> dict[str, Any]:
    """Inline-кнопки главного меню (по 2 в ряд)."""
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Обзор", "callback_data": "adm:over"},
                {"text": "🌐 Парсинг", "callback_data": "adm:parse:24h"},
            ],
            [
                {"text": "👥 Пользователи", "callback_data": "adm:users"},
                {"text": "🗄 База", "callback_data": "adm:db"},
            ],
            [
                {"text": "🎫 Билеты", "callback_data": "adm:tickets"},
                {"text": "🔔 Подписки", "callback_data": "adm:subs"},
            ],
            [
                {"text": "📱 Телефоны", "callback_data": "adm:ph"},
                {"text": "🛟 Бэкапы", "callback_data": "adm:backup"},
            ],
            [
                {"text": "🔄 Обновить", "callback_data": "adm:home"},
            ],
        ]
    }


def make_section_keyboard(section: str, period: str = "24h") -> dict[str, Any]:
    """Inline-кнопки внутри раздела."""
    if section in ("ph", "phones"):
        return {
            "inline_keyboard": [
                [
                    {"text": "◀ Назад", "callback_data": "adm:home"},
                    {"text": "🔄 Обновить", "callback_data": "adm:ph"},
                ],
            ]
        }
    if section == "parse":
        p24 = "• 24 ч •" if period == "24h" else "24 ч"
        p7 = "• 7 дн •" if period == "7d" else "7 дн"
        return {
            "inline_keyboard": [
                [
                    {"text": p24, "callback_data": "adm:parse:24h"},
                    {"text": p7, "callback_data": "adm:parse:7d"},
                ],
                [
                    {"text": "🩺 Здоровье", "callback_data": "adm:health"},
                    {"text": "🧭 Порядок телефонов", "callback_data": "adm:ord"},
                ],
                [
                    {"text": "◀ Назад", "callback_data": "adm:home"},
                    {"text": "🔄 Обновить", "callback_data": f"adm:parse:{period}"},
                ],
            ]
        }
    if section == "health":
        return {
            "inline_keyboard": [
                [
                    {"text": "◀ К парсингу", "callback_data": "adm:parse:24h"},
                    {"text": "🔄 Обновить", "callback_data": "adm:health"},
                ],
            ]
        }
    if section == "ord":
        return {
            "inline_keyboard": [
                [
                    {"text": "◀ К парсингу", "callback_data": "adm:parse:24h"},
                    {"text": "🔄 Обновить", "callback_data": "adm:ord"},
                ],
            ]
        }
    if section == "backup":
        return {
            "inline_keyboard": [
                [{"text": "💾 Сделать бэкап сейчас", "callback_data": "adm:backup_now"}],
                [
                    {"text": "◀ Назад", "callback_data": "adm:home"},
                    {"text": "🔄 Обновить", "callback_data": "adm:backup"},
                ],
            ]
        }
    return {
        "inline_keyboard": [
            [
                {"text": "◀ Назад", "callback_data": "adm:home"},
                {"text": "🔄 Обновить", "callback_data": f"adm:{section}"},
            ],
        ]
    }


def build_home() -> str:
    """Главный экран панели разработчика."""
    return (
        "🛠 <b>Панель разработчика</b>\n\n"
        "Выберите раздел для просмотра информации:"
    )


def build_overview(ctx: dict[str, Any]) -> str:
    """Раздел 1: 📊 Обзор."""
    lines = ["📊 <b>Обзор</b>", ""]

    uptime_s = ctx.get("uptime_seconds", 0.0)
    lines.append(f"Аптайм: {format_uptime(uptime_s)}")

    git_ver = ctx.get("git_version") or "—"
    lines.append(f"Версия: {escape(str(git_ver))}")

    u_tot = ctx.get("users_total", 0)
    u_24 = ctx.get("users_active_24h", 0)
    u_7d = ctx.get("users_active_7d", 0)
    u_new = ctx.get("users_new_24h", 0)
    lines.append(
        f"Людей: {u_tot} всего · активных 24 ч: {u_24} · 7 дн: {u_7d} · новых 24 ч: {u_new}"
    )

    s_today = ctx.get("searches_today", 0)
    bg_info = ctx.get("bg_searches", "выключен")
    lines.append(f"Поисков сегодня: людей — {s_today}, фоновых — {escape(str(bg_info))}")

    bg_pairs = ctx.get("bg_active_pairs", ctx.get("bg_pairs"))
    bg_routes = ctx.get("bg_routes", ctx.get("bg_distinct_routes"))
    if bg_pairs is not None and bg_routes is not None and str(bg_info) != "выключен":
        lines.append(
            f"Фоновый сбор: {escape(str(bg_pairs))} активных пар (маршрут+дата) · "
            f"{escape(str(bg_routes))} маршрутов · "
            f"{escape(str(bg_info))} сделано сегодня"
        )

    active_subs = ctx.get("active_subs", 0)
    lines.append(f"Активных подписок: {active_subs}")

    busy_cnt = ctx.get("busy_searches", 0)
    lines.append(f"Поисков сейчас: {busy_cnt}")

    queue_data = ctx.get("search_queue") or ctx.get("gate") or ctx.get("prio_gate")
    if queue_data is not None:
        if hasattr(queue_data, "waiting_by_priority") and callable(queue_data.waiting_by_priority):
            x = queue_data.active()
            n = queue_data.capacity
            w = queue_data.waiting_by_priority()
            a = w.get(0, 0)
            b = w.get(1, 0)
            c = w.get(2, 0)
            lines.append(f"Очередь поисков: идёт {x} из {n}, ждут: люди {a}, подписки {b}, фон {c}")
        elif isinstance(queue_data, dict):
            x = queue_data.get("active", 0)
            n = queue_data.get("capacity", 0)
            w = queue_data.get("waiting") or queue_data.get("waiting_by_priority") or {}
            a = queue_data.get("people", w.get(0, 0))
            b = queue_data.get("subs", w.get(1, 0))
            c = queue_data.get("bg", w.get(2, 0))
            lines.append(f"Очередь поисков: идёт {x} из {n}, ждут: люди {a}, подписки {b}, фон {c}")
        elif isinstance(queue_data, str):
            if not queue_data.startswith("Очередь поисков:"):
                lines.append(f"Очередь поисков: {queue_data}")
            else:
                lines.append(queue_data)

    last_b = ctx.get("last_backup_at")
    now_ts = ctx.get("now", time.time())
    lines.append(f"Последний бэкап: {format_backup_time(last_b, now_ts)}")

    state_mb = ctx.get("state_size_mb", 0.0)
    lines.append(f"Папка state: {state_mb:.1f} МБ")

    return truncate_text("\n".join(lines))


def build_parse(ctx: dict[str, Any], period: str = "24h") -> str:
    """Раздел 2: 🌐 Парсинг."""
    period_title = "7 дн" if period == "7d" else "24 ч"
    lines = [f"🌐 <b>Парсинг</b> ({period_title})", ""]

    summary = ctx.get("summary")
    if not summary or summary.get("_total", {}).get("runs", 0) == 0:
        lines.append("Данных пока нет: метрики копятся с момента включения")
        if ctx.get("sub_prefilter_line"):  # все сборы подписок пропущены — пропуски всё равно видны
            lines.extend(["", ctx["sub_prefilter_line"]])
        return "\n".join(lines)

    settings = ctx.get("settings")
    if settings is None:
        try:
            from .config import load_settings
            settings = load_settings()
        except Exception:
            settings = None

    trial_sites: set[str] = set()
    try:
        trial_stage_enabled = getattr(settings, "trial_stage", True) if settings else True
        if ctx.get("trial_sites") is not None:
            trial_sites = set(ctx.get("trial_sites"))
        elif settings and trial_stage_enabled:
            trial_sites = {
                s.name for s in getattr(settings, "sites", [])
                if getattr(s, "stage", "live") == "trial"
            }
    except Exception as e:
        log.warning("Ошибка определения trial_sites в build_parse: %s", e)
        trial_sites = set()

    statuses = ctx.get("parser_statuses")
    if statuses is None and summary:
        from .parser_watch import evaluate_status
        min_runs = getattr(settings, "parser_watch_min_runs", 8)
        min_ok = getattr(settings, "parser_watch_min_ok_pct", 60.0)
        max_inc = getattr(settings, "parser_watch_max_incomplete_pct", 60.0)
        statuses = {}
        for s_name, item in summary.items():
            if s_name != "_total":
                runs = item.get("runs", 0)
                ok = item.get("ok", 0)
                part = item.get("partial", 0)
                statuses[s_name] = evaluate_status(
                    runs=runs,
                    ok=ok,
                    partial=part,
                    min_runs=min_runs,
                    min_ok_pct=float(min_ok),
                    max_incomplete_pct=float(max_inc),
                )

    if statuses:
        status_parts = []
        for s, st in sorted(statuses.items()):
            label = f"{s} (пробный)" if s in trial_sites else s
            status_parts.append(f"{escape(label)}: {escape(st)}")
        lines.append(f"Статус: {' · '.join(status_parts)}")
        lines.append("")

    def _fmt_site(name: str, item: dict[str, Any]) -> str:
        runs = item.get("runs", 0)
        ok = item.get("ok", 0)
        pct = round(ok / runs * 100) if runs > 0 else 0
        part = item.get("partial", 0)
        capt = item.get("captcha", 0)
        fail = item.get("failed", 0)
        t = item.get("avg_elapsed", 0.0)
        v = item.get("avg_offers", 0.0)
        return (
            f"{escape(name)}: {runs} запусков · ок {pct}% · "
            f"неполных {part} · капч {capt} · ошибок {fail} · "
            f"ср. время {t:.1f} с · ср. вариантов {v:.1f}"
        )

    tot = summary.get("_total")
    if tot:
        lines.append(_fmt_site("Итого", tot))

    sites = [k for k in sorted(summary.keys()) if k != "_total"]
    for s in sites:
        label = f"{s} (пробный)" if s in trial_sites else s
        lines.append(_fmt_site(label, summary[s]))

    counts = ctx.get("counts_by_source", {})
    u_c = counts.get("user", 0)
    s_c = counts.get("sub", 0)
    b_c = counts.get("bg", 0)
    lines.append("")
    lines.append("<b>Источники:</b>")
    lines.append(f"Люди: {u_c} · Подписки: {s_c} · Фон: {b_c}")
    if ctx.get("sub_prefilter_line"):  # КТ4, работа 2: только при SUB_PREFILTER=1 и TP_TOKEN
        lines.append(ctx["sub_prefilter_line"])

    devs = ctx.get("devices_summary") or ctx.get("by_device") or {}
    site_devs = ctx.get("site_devices_summary") or ctx.get("by_site_device") or {}

    lines.append("")
    if not devs:
        lines.append("Телефоны: данных пока нет")
    else:
        lines.append("<b>Телефоны:</b>")
        for dev, item in sorted(devs.items()):
            runs = item.get("runs", 0)
            ok = item.get("ok", 0)
            pct = round(ok / runs * 100) if runs > 0 else 0
            part = item.get("partial", 0)
            capt = item.get("captcha", 0)
            t = item.get("avg_fetch", 0.0)
            w = item.get("avg_queue_wait", 0.0)
            lines.append(
                f"{escape(str(dev))}: {runs} запусков · ок {pct}% · "
                f"неполных {part} · капч {capt} · ср. запрос {t:.1f} с · ср. ожидание {w:.1f} с"
            )

        pairs = [
            (k, v) for k, v in site_devs.items()
            if isinstance(v, dict) and v.get("runs", 0) >= 3
        ]
        if pairs:
            pairs.sort(key=lambda item: item[1].get("runs", 0), reverse=True)
            lines.append("")
            lines.append("<b>Сайт × телефон:</b>")
            for (s_name, dev_name), item in pairs:
                runs = item.get("runs", 0)
                ok = item.get("ok", 0)
                pct = round(ok / runs * 100) if runs > 0 else 0
                part = item.get("partial", 0)
                capt = item.get("captcha", 0)
                t = item.get("avg_fetch", 0.0)
                w = item.get("avg_queue_wait", 0.0)
                pair_line = (
                    f"{escape(str(s_name))} × {escape(str(dev_name))}: {runs} · "
                    f"ок {pct}% · неполн. {part} · капч {capt} · {t:.1f} с"
                )
                if len("\n".join(lines)) + len(pair_line) + 2 > 3700:
                    break
                lines.append(pair_line)

    errors = ctx.get("recent_errors", [])
    lines.append("")
    lines.append("<b>Последние ошибки:</b>")
    if errors:
        for err in errors[:5]:
            ts = err.get("ts", 0.0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M")
            site_name = escape(str(err.get("site", "—")))
            raw_err = str(err.get("error", "—"))
            if len(raw_err) > 80:
                raw_err = raw_err[:80] + "…"
            lines.append(f"• {time_str} {site_name}: {escape(raw_err)}")
    else:
        lines.append("—")

    try:
        storage_dir = getattr(settings, "storage_dir", None)
        rules_path = Path(storage_dir) / "stop_rules.json" if storage_dir else Path("state/stop_rules.json")
        if rules_path.is_file():
            import json
            from .stop_calibrate import format_report as format_stop_report
            data = json.loads(rules_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                report_str = format_stop_report(data)
                if report_str:
                    lines.append("")
                    lines.append(report_str)
    except Exception as e:
        log.warning("Ошибка чтения stop_rules.json в build_parse: %s", e)

    return truncate_text("\n".join(lines))


def build_phone_order(ctx: dict[str, Any]) -> str:
    """Подраздел «Порядок телефонов» (вынесен из Парсинга, чтобы влезало в сообщение)."""
    lines = ["🧭 <b>Порядок телефонов</b>"]
    # Блок: Порядок телефонов по каждому сайту
    settings = ctx.get("settings")
    if settings is None:
        try:
            from .config import load_settings
            settings = load_settings()
        except Exception:
            settings = None

    if settings and getattr(settings, "sites", None):
        try:
            from .droidhub import candidates, _get_registry, _get_phones_hold
            from .speed import get_speed_table
            speed_table = get_speed_table(settings)
            reg = getattr(settings, "phones", None) or _get_registry(settings)
            reg_all = reg.all() if reg else {}
            phones_hold = _get_phones_hold(settings)

            phone_sites = [s for s in settings.sites if getattr(s, "phone", None)]
            if phone_sites:
                order_lines = ["", "<b>Порядок телефонов:</b>"]
                for s in phone_sites:
                    live_cands = list(candidates(settings, s, priority=0))
                    bg_cands = list(candidates(settings, s, priority=1))

                    raw_phone = (getattr(s, "phone", None) or "").strip()
                    site_pool: list[str] = []
                    if raw_phone == "auto":
                        base_devs = [d.strip() for d in (getattr(settings, "droidhub_devices", None) or []) if d and d.strip()]
                        for d in base_devs:
                            if d not in site_pool:
                                site_pool.append(d)
                    elif "," in raw_phone:
                        for p in raw_phone.split(","):
                            clean_p = p.strip()
                            if clean_p and clean_p not in site_pool:
                                site_pool.append(clean_p)
                    elif raw_phone:
                        site_pool.append(raw_phone)

                    phone_extend = getattr(s, "phone_extend", True)
                    if phone_extend is None:
                        phone_extend = True
                    if phone_extend and (raw_phone == "auto" or "," in raw_phone):
                        def _fs_key(item: tuple[str, dict[str, Any]]) -> tuple:
                            fs = item[1].get("first_seen")
                            if isinstance(fs, (int, float)):
                                return (0, float(fs))
                            if isinstance(fs, str):
                                try:
                                    return (0, float(fs))
                                except ValueError:
                                    return (1, fs)
                            return (2, str(fs or ""))

                        for dev_id, entry in sorted(reg_all.items(), key=_fs_key):
                            clean_id = dev_id.strip()
                            if not clean_id or clean_id in site_pool:
                                continue
                            if entry.get("status") == "new":
                                continue
                            allow = entry.get("sites_allow")
                            if allow is not None and s.name not in allow:
                                continue
                            deny = entry.get("sites_deny") or []
                            if s.name in deny:
                                continue
                            site_pool.append(clean_id)

                    all_live = live_cands + [d for d in site_pool if d not in live_cands]
                    all_bg = bg_cands + [d for d in site_pool if d not in bg_cands]
                    if not all_live and not all_bg:
                        continue

                    def _fmt_cand(dev: str, c_name: str) -> str:
                        st = speed_table.get_stats(s.name, dev, c_name)
                        pst = speed_table.get_pair_status(s.name, dev)
                        p_info = reg_all.get(dev, {})
                        med_str = f"{st.median_fetch:.1f} с" if st.median_fetch is not None else "—"
                        pct_str = f"{round(st.ok_rate * 100)}%" if st.n > 0 else "—"
                        badges = []
                        if pst.in_quarantine:
                            badges.append("карантин")
                        elif pst.awaiting_trial:
                            badges.append("пробный")
                        if p_info.get("low_battery"):
                            badges.append("пониженная батарея")
                        if dev in phones_hold or p_info.get("status") == "hold" or p_info.get("is_hold_env"):
                            badges.append("заморозка")
                        elif p_info.get("status") == "new":
                            badges.append("ожидает проверки")
                        badge_str = f" [{', '.join(badges)}]" if badges else ""
                        return f"{escape(dev)} ({med_str} · {pct_str} · n={st.n}){badge_str}"

                    live_str = ", ".join(_fmt_cand(d, "live") for d in all_live) if all_live else "нет"
                    bg_str = ", ".join(_fmt_cand(d, "bg") for d in all_bg) if all_bg else "нет"

                    order_lines.append(f"<b>{escape(s.name)}:</b>")
                    order_lines.append(f"• люди: {live_str}")
                    order_lines.append(f"• фон: {bg_str}")

                if len(order_lines) > 2:
                    for ol in order_lines:
                        if len("\n".join(lines)) + len(ol) + 2 > 3700:
                            break
                        lines.append(ol)
        except Exception:
            pass

    if len(lines) == 1:
        lines.append("Данных пока нет")
    return truncate_text("\n".join(lines))


def build_users(ctx: dict[str, Any]) -> str:
    """Раздел 3: 👥 Пользователи."""
    lines = ["👥 <b>Пользователи</b>", ""]

    tot = ctx.get("total", 0)
    blocked = ctx.get("blocked", 0)
    new_24 = ctx.get("new_24h", 0)
    new_7d = ctx.get("new_7d", 0)
    act_24 = ctx.get("active_24h", 0)
    act_7d = ctx.get("active_7d", 0)
    searches = ctx.get("searches", 0)

    lines.append(f"Всего: {tot}")
    lines.append(f"Заблокировано: {blocked}")
    lines.append(f"Новых: за 24 ч — {new_24}, за 7 дн — {new_7d}")
    lines.append(f"Активных: за 24 ч — {act_24}, за 7 дн — {act_7d}")
    lines.append(f"Поисков всего: {searches}")

    top = ctx.get("top_searchers", [])
    lines.append("")
    lines.append("<b>Топ-5 по числу поисков:</b>")
    if top:
        for u in top[:5]:
            name = f"@{escape(u.username)}" if u.username else escape(u.first_name or "—")
            mark = " 🚫" if u.blocked else ""
            lines.append(f"• {u.tg_id} {name}: {u.total_searches}{mark}")
    else:
        lines.append("—")

    recent = ctx.get("recent_users", [])
    lines.append("")
    lines.append("<b>Последние 5:</b>")
    if recent:
        for u in recent[:5]:
            name = f"@{escape(u.username)}" if u.username else escape(u.first_name or "—")
            time_str = datetime.fromtimestamp(u.last_seen_at).strftime("%d.%m %H:%M")
            mark = " 🚫" if u.blocked else ""
            lines.append(f"• {u.tg_id} {name} · {time_str} · поисков: {u.total_searches}{mark}")
    else:
        lines.append("—")

    return truncate_text("\n".join(lines))


def build_db(ctx: dict[str, Any]) -> str:
    """Раздел 4: 🗄 База."""
    lines = ["🗄 <b>База</b>", ""]

    lines.append("<b>Размер файлов:</b>")
    db_files = ctx.get("db_files", [])
    if db_files:
        for f in db_files:
            lines.append(f"• {escape(f['name'])}: {f['size_mb']:.1f} МБ")
    else:
        lines.append("—")

    lines.append("")
    lines.append("<b>Строк в таблицах:</b>")
    h_stats = ctx.get("history_stats", {})
    ph_cnt = h_stats.get("price_history", 0)
    rd_cnt = h_stats.get("route_daily", 0)
    hb_cnt = h_stats.get("scheduler_heartbeat", 0)
    lines.append(f"• price_history: {ph_cnt:,} (route_daily: {rd_cnt:,}, heartbeat: {hb_cnt:,})".replace(",", " "))

    s_stats = ctx.get("subscriptions_stats", {})
    sub_tot = s_stats.get("subscriptions", 0)
    sub_act = s_stats.get("active_subscriptions", 0)
    sp_cnt = s_stats.get("sub_prices", 0)
    not_cnt = s_stats.get("notifications", 0)
    lines.append(
        f"• subscriptions: {sub_tot:,} (активных {sub_act:,}), "
        f"sub_prices: {sp_cnt:,}, notifications: {not_cnt:,}".replace(",", " ")
    )

    u_cnt = ctx.get("users_count", 0)
    lines.append(f"• users: {u_cnt:,}".replace(",", " "))

    m_cnt = ctx.get("metrics_runs_count", 0)
    lines.append(f"• site_runs: {m_cnt:,}".replace(",", " "))

    lines.append("")
    lines.append("<b>История цен:</b>")
    e_date = h_stats.get("earliest_date")
    l_date = h_stats.get("latest_date")
    if e_date and l_date:
        lines.append(f"• Период: с {e_date.strftime('%d.%m.%Y')} по {l_date.strftime('%d.%m.%Y')}")
    else:
        lines.append("• Период: —")

    growth_24 = ctx.get("history_growth_24h", 0)
    growth_mb = ctx.get("growth_mb_day", 0.0)
    lines.append(f"• Прирост за 24 ч: +{growth_24} строк (~{growth_mb:.2f} МБ/сут)")

    disk_free = ctx.get("disk_free_gb", 0.0)
    lines.append("")
    lines.append(f"Свободно на диске: {disk_free:.1f} ГБ")

    return truncate_text("\n".join(lines))


def build_tickets(ctx: dict[str, Any]) -> str:
    """Раздел 5: 🎫 Билеты."""
    lines = ["🎫 <b>Билеты</b> (7 дней)", ""]

    r_cnt = ctx.get("routes_count", 0)
    s_cnt = ctx.get("searches_count", 0)
    lines.append(f"Маршрутов: {r_cnt} · Замеров: {s_cnt}")

    avg_days = ctx.get("avg_days_to_departure")
    avg_str = f"{avg_days:.1f} дн" if avg_days is not None else "—"
    lines.append(f"Ср. дней до вылета: {avg_str}")

    top_routes = ctx.get("popular_routes", [])
    lines.append("")
    lines.append("<b>Топ-5 популярных маршрутов:</b>")
    if top_routes:
        for r in top_routes[:5]:
            orig = escape(city_name(r["origin"]))
            dest = escape(city_name(r["destination"]))
            cnt = r["count"]
            min_p = r.get("min_price")
            p_str = f"от {min_p:,} ₽".replace(",", " ") if min_p is not None else "цена —"
            lines.append(f"• {orig} → {dest}: {cnt} замеров ({p_str})")
    else:
        lines.append("—")

    bg = ctx.get("baggage", {})
    tot_bg = bg.get("total", 0)
    lines.append("")
    lines.append("<b>Багаж:</b>")
    if tot_bg > 0:
        w_pct = round(bg.get("with", 0) / tot_bg * 100)
        wo_pct = round(bg.get("without", 0) / tot_bg * 100)
        un_pct = round(bg.get("unknown", 0) / tot_bg * 100)
        lines.append(f"С багажом {w_pct}% · Без багажа {wo_pct}% · Неизвестно {un_pct}%")
    else:
        lines.append("—")

    sites = ctx.get("site_share", [])
    lines.append("")
    lines.append("<b>Распределение по сайтам:</b>")
    if sites:
        for site, rows, wins in sites:
            lines.append(f"• {escape(site)}: {rows} записей · {wins} раз мин. цена")
    else:
        lines.append("—")

    fc = ctx.get("forecast")
    if fc is not None or "forecast" in ctx:
        lines.append("")
        lines.extend(format_forecast_block(fc))

    bg_pairs = ctx.get("bg_active_pairs", ctx.get("bg_pairs"))
    bg_routes = ctx.get("bg_routes", ctx.get("bg_distinct_routes"))
    bg_done = ctx.get("bg_done", ctx.get("bg_searches"))
    if bg_pairs is not None and bg_routes is not None and bg_done and str(bg_done) != "выключен":
        lines.append("")
        lines.append("<b>Фоновый сбор:</b>")
        lines.append(
            f"• Активных пар (маршрут+дата): {escape(str(bg_pairs))}\n"
            f"• Различных маршрутов: {escape(str(bg_routes))}\n"
            f"• Сделано сегодня: {escape(str(bg_done))}"
        )

    return truncate_text("\n".join(lines))


def format_forecast_block(fc: dict[str, Any] | None) -> list[str]:
    """Форматирует блок «Прогноз» для панели администратора."""
    lines = ["<b>Прогноз:</b>"]
    if not fc:
        lines.append("Модель ещё не обучалась")
        return lines

    trained_at = fc.get("trained_at")
    if trained_at:
        try:
            dt = datetime.fromisoformat(str(trained_at))
            trained_str = dt.strftime("%d.%m %H:%M")
        except Exception:
            trained_str = str(trained_at)
    else:
        trained_str = "—"

    train_rows = fc.get("train_rows", 0)
    test_rows = fc.get("test_rows", 0)
    routes_cnt = fc.get("routes_count")
    if routes_cnt is None:
        tr = fc.get("train_routes", 0)
        te = fc.get("test_routes", 0)
        routes_cnt = tr + te if (tr + te) > 0 else 0
    departed_flights = fc.get("departed_flights_count", 0)
    verdict = fc.get("verdict", "—")

    lines.append(f"• Обучение: {escape(str(trained_str))}")
    lines.append(f"• Строк: обучение {train_rows:,} · проверка {test_rows:,}".replace(",", " "))
    lines.append(f"• Маршрутов: {routes_cnt} · Вылетевших рейсов с ответами: {departed_flights}")

    v_raw = str(verdict)
    if v_raw in ("лучше правил", "модель лучше"):
        v_text = "лучше правил"
    elif v_raw in ("не лучше правил", "модель не лучше правил"):
        v_text = "не лучше правил"
    else:
        v_text = v_raw
    lines.append(f"• Вердикт: {escape(v_text)}")

    metrics = fc.get("metrics")
    if metrics and isinstance(metrics, dict):
        m = metrics.get("model")
        b1 = metrics.get("baseline_always_buy")
        b2 = metrics.get("baseline_rules")
        if m:
            acc = round(m.get("accuracy", 0.0) * 100)
            prec = round(m.get("precision", 0.0) * 100)
            rec = round(m.get("recall", 0.0) * 100)
            miss = m.get("missed_opportunity_pct", 0.0)
            lines.append(
                f"• Модель: точность {acc}% · точность/полнота {prec}%/{rec}% · упущенная выгода {miss:.1f}%"
            )
        if b1 and b2:
            acc1 = round(b1.get("accuracy", 0.0) * 100)
            miss1 = b1.get("missed_opportunity_pct", 0.0)
            acc2 = round(b2.get("accuracy", 0.0) * 100)
            miss2 = b2.get("missed_opportunity_pct", 0.0)
            lines.append(
                f"• Базовые: «всегда брать» {acc1}% (упущ. {miss1:.1f}%), правила {acc2}% (упущ. {miss2:.1f}%)"
            )

    return lines


def build_forecast(ctx: dict[str, Any]) -> str:
    """Раздел / блок: 🔮 Прогноз."""
    lines = ["🔮 <b>Прогноз</b>", ""]
    fc = ctx.get("forecast", ctx)
    lines.extend(format_forecast_block(fc))
    return truncate_text("\n".join(lines))


def build_subs(ctx: dict[str, Any]) -> str:
    """Раздел 6: 🔔 Подписки."""
    lines = ["🔔 <b>Подписки</b>", ""]

    act = ctx.get("active_count", 0)
    groups = ctx.get("groups_count", 0)
    lines.append(f"Активных: {act} (на {groups} маршрутов)")

    n_24 = ctx.get("notifications_24h", 0)
    lines.append(f"Уведомлений за 24 ч: {n_24}")

    top = ctx.get("top_routes", [])
    lines.append("")
    lines.append("<b>Топ-5 маршрутов:</b>")
    if top:
        for orig, dest, cnt in top[:5]:
            lines.append(f"• {escape(city_name(orig))} → {escape(city_name(dest))}: {cnt}")
    else:
        lines.append("—")

    ov = ctx.get("overdue", {})
    lines.append("")
    lines.append("<b>Очередь проверок:</b>")
    lines.append(
        f"Всего: {ov.get('total', 0)} · Просрочено: {ov.get('overdue', 0)} · "
        f"Макс. отставание: {ov.get('max_lag_minutes', 0)} мин"
    )

    reasons = ctx.get("reasons", {})
    lines.append("")
    lines.append("<b>Уведомления за 7 дней по причинам:</b>")
    if reasons:
        for r_name, r_cnt in reasons.items():
            lines.append(f"• {escape(r_name)}: {r_cnt}")
    else:
        lines.append("—")

    return truncate_text("\n".join(lines))


def build_backups(ctx: dict[str, Any]) -> str:
    """Раздел 7: 🛟 Бэкапы."""
    lines = ["🛟 <b>Бэкапы</b>", ""]

    keep = ctx.get("keep", 14)
    hour = ctx.get("hour", 3)
    lines.append(f"<b>Политика:</b>\nХранится копий: {keep} · Автобэкап в {hour:02d}:00")

    lines.append("")
    lines.append("<b>Последние копии:</b>")
    backups = ctx.get("backups", [])
    if backups:
        for b in backups[:6]:
            db_name = escape(str(b.get("db", b.get("name", "db"))))
            size = b.get("size", 0)
            size_mb = size / (1024 * 1024)
            size_str = f"{size_mb:.1f} МБ" if size_mb >= 0.1 else f"{size / 1024:.0f} КБ"
            time_str = datetime.fromtimestamp(b.get("created_at", 0)).strftime("%d.%m %H:%M")
            lines.append(f"• {db_name} ({size_str}, {time_str})")
    else:
        lines.append("Копий пока нет")

    return truncate_text("\n".join(lines))


def build_phones(ctx: dict[str, Any]) -> str:
    """Раздел: 📱 Телефоны хаба."""
    lines = ["📱 <b>Телефоны хаба</b>", ""]
    phones = ctx.get("phones", [])
    if not phones:
        lines.append("Телефонов пока нет в реестре.")
    else:
        for p in phones:
            p_id = escape(str(p.get("id", "")))
            p_name = escape(str(p.get("name", p_id)))
            lines.append(f"<b>{p_name}</b> (<code>{p_id}</code>)")

            # Статус
            if p.get("is_hold_env"):
                st_str = "заморожен (env) ❄️"
            elif p.get("checking"):
                st_str = "⏳ проверяется"
            elif p.get("status") == "active":
                st_str = "в работе ✅"
            elif p.get("status") == "new":
                st_str = "ожидает проверки ⏳"
            else:
                st_str = "заморожен ⏸"
            lines.append(f"• Статус: {st_str}")

            # Связь
            online = p.get("online")
            is_fresh = p.get("is_online_fresh", False)
            if online is True:
                conn_str = "онлайн 🟢"
            elif online is False and is_fresh:
                conn_str = "офлайн 🔴"
            elif online is False:
                conn_str = "офлайн (снимок устарел)"
            else:
                conn_str = "неизвестно"
            lines.append(f"• Связь: {conn_str}")

            # Батарея
            batt = p.get("battery")
            charging = p.get("charging", False)
            ch_str = " ⚡" if charging else ""
            batt_str = f"{batt}%{ch_str}" if batt is not None else "—"
            if p.get("low_battery"):
                batt_str += " (пониженная батарея 🪫)"
            lines.append(f"• Батарея: {batt_str}")

            # Сайты
            allow = p.get("sites_allow")
            deny = p.get("sites_deny")
            if allow:
                lines.append(f"• Разрешены сайты: {escape(', '.join(allow))}")
            elif deny:
                lines.append(f"• Запрещены сайты: {escape(', '.join(deny))}")
            else:
                lines.append("• Сайты: все")

            # Когда добавлен
            first_seen = p.get("first_seen")
            if isinstance(first_seen, (int, float)) and first_seen > 0:
                dt_str = datetime.fromtimestamp(first_seen).strftime("%d.%m %H:%M")
            elif first_seen:
                dt_str = escape(str(first_seen))
            else:
                dt_str = "—"
            lines.append(f"• Добавлен: {dt_str}")
            note = p.get("note")
            if note:
                lines.append(f"• Примечание: {escape(str(note))}")
            lines.append("")

    lines.append("Команды: <code>/phone &lt;id&gt; test|hold|on|all|allow s1,s2|deny s1</code>")
    return truncate_text("\n".join(lines))


def make_phones_keyboard(phones: list[dict[str, Any]]) -> dict[str, Any]:
    """Inline-кнопки управления телефонами: переключение статуса и назад."""
    rows: list[list[dict[str, str]]] = []
    for p in phones:
        p_id = str(p.get("id", "")).strip()
        p_name = str(p.get("name", p_id)).strip() or p_id
        p_name = p_name.replace(" (m)", "")
        is_hold_env = bool(p.get("is_hold_env", False))
        status = str(p.get("status", "active"))
        if is_hold_env:
            # Для заморозки через env кнопку возврата не показываем
            continue
        if status == "new":
            btn = {
                "text": f"🔍 Проверить {p_name}",
                "callback_data": f"adm:ph:test:{p_id}",
            }
            rows.append([btn])
        else:
            if status == "active":
                btn_toggle = {
                    "text": f"❄️ {p_name}",
                    "callback_data": f"adm:ph:hold:{p_id}",
                }
            else:
                btn_toggle = {
                    "text": f"▶️ {p_name}",
                    "callback_data": f"adm:ph:on:{p_id}",
                }
            btn_recheck = {
                "text": "🔄",
                "callback_data": f"adm:ph:re:{p_id}",
            }
            # Кнопка статуса с именем длиннее, поэтому Telegram делает её шире
            rows.append([btn_toggle, btn_recheck])

    rows.append(
        [
            {"text": "◀ Назад", "callback_data": "adm:home"},
            {"text": "🔄 Обновить", "callback_data": "adm:ph"},
        ]
    )
    return {"inline_keyboard": rows}

