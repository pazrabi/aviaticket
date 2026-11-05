"""Модуль проверки телефонов хаба перед допуском к работе."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
import logging
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Any

from .config import Settings, SiteConfig
from .droidhub import DroidhubError, fetch_page_snapshot
from .metrics import MetricsStore
from .models import Query
from .parsers import PARSERS
from .pipeline import filter_wrong_dates, resolve_extra
from .sites import build_url

logger = logging.getLogger(__name__)

# Контрольный маршрут по умолчанию: MOW -> LED, вылет через ~35 дней, 1 взрослый, эконом
DEFAULT_CONTROL_ORIGIN = "MOW"
DEFAULT_CONTROL_DESTINATION = "LED"
DEFAULT_CONTROL_DAYS_AHEAD = 35

# Таймауты
MAX_OVERALL_CHECK_SECONDS = 900.0  # всего не более ~15 минут

# Словарь активных проверок: phone_id -> asyncio.Task (хранится только в памяти)
_active_checks: dict[str, asyncio.Task] = {}


def is_checking(phone_id: str) -> bool:
    """True, если телефон сейчас проходит проверку."""
    clean_id = str(phone_id).strip()
    task = _active_checks.get(clean_id)
    if task is not None:
        if task.done():
            _active_checks.pop(clean_id, None)
            return False
        return True
    return False


def set_checking(phone_id: str, checking: bool) -> None:
    """Устанавливает или сбрасывает флаг проверки для телефона."""
    clean_id = str(phone_id).strip()
    if not checking:
        _active_checks.pop(clean_id, None)


@dataclass
class PassResult:
    """Результат одного прохода проверки сайта."""
    pass_num: int
    ok: bool = False
    elapsed: float = 0.0
    captcha: bool = False
    offers_count: int = 0
    min_price: int | None = None
    results_complete: bool = True
    error: str | None = None


@dataclass
class SiteCheckResult:
    """Итог проверки одного сайта (2 прохода)."""
    site_name: str
    passes: list[PassResult] = field(default_factory=list)
    verdict: str = "не ок"  # "ок" или "не ок"
    reason: str = ""        # 502, капча, пусто, неполно, цена расходится
    ref_price: float | None = None
    price_note: str = ""


@dataclass
class PhoneCheckReport:
    """Полный отчёт проверки телефона."""
    phone_id: str
    phone_name: str
    site_results: dict[str, SiteCheckResult] = field(default_factory=dict)
    ok_sites: list[str] = field(default_factory=list)
    failed_sites: list[str] = field(default_factory=list)
    unresponsive: bool = False
    admitted: bool = False
    message: str = ""


def get_reference_price(
    settings: Settings,
    site_name: str,
    query: Query,
    exclude_device: str | None = None,
    now: float | None = None,
) -> float | None:
    """Медиана минимальных цен за последние 24 ч из истории/метрик у других телефонов."""
    cur_now = time.time() if now is None else float(now)
    since_ts = cur_now - 86400.0
    prices: list[float] = []

    # 1. Поиск в metrics.sqlite (site_runs.min_price)
    metrics_path = Path(settings.storage_dir) / "metrics.sqlite"
    if metrics_path.exists():
        try:
            with sqlite3.connect(metrics_path, timeout=5) as db:
                cols = {row[1] for row in db.execute("PRAGMA table_info(site_runs)").fetchall()}
                if "min_price" in cols:
                    query_sql = (
                        "SELECT min_price FROM site_runs WHERE site = ? AND source = 'phone_check' "
                        "AND min_price IS NOT NULL AND min_price > 0 AND ts >= ?"
                    )
                    params: list[Any] = [site_name, since_ts]
                    if "device" in cols and exclude_device:
                        query_sql += " AND (device IS NULL OR device != ?)"
                        params.append(exclude_device)
                    rows = db.execute(query_sql, params).fetchall()
                    for (p,) in rows:
                        if p and p > 0:
                            prices.append(float(p))
        except Exception as e:
            logger.debug("Ошибка чтения эталона из metrics: %s", e)

    # 2. Поиск в history.sqlite (price_history)
    history_path = Path(settings.storage_dir) / "history.sqlite"
    if history_path.exists():
        try:
            with sqlite3.connect(history_path, timeout=5) as db:
                cols = {row[1] for row in db.execute("PRAGMA table_info(price_history)").fetchall()}
                if "price" in cols and "observed_at" in cols:
                    q_sql = (
                        "SELECT MIN(price) FROM price_history WHERE site = ? AND origin = ? "
                        "AND destination = ? AND depart = ? AND observed_at >= ?"
                    )
                    params = [site_name, query.origin, query.destination, query.depart.isoformat(), since_ts]
                    if "device" in cols and exclude_device:
                        q_sql += " AND (device IS NULL OR device != ?)"
                        params.append(exclude_device)
                    if "search_id" in cols:
                        q_sql += " GROUP BY search_id"
                    else:
                        q_sql += " GROUP BY observed_at"
                    rows = db.execute(q_sql, params).fetchall()
                    for (p,) in rows:
                        if p and p > 0:
                            prices.append(float(p))
        except Exception as e:
            logger.debug("Ошибка чтения эталона из history: %s", e)

    if prices:
        return float(statistics.median(prices))
    return None


async def run_site_pass(
    settings: Settings,
    site: SiteConfig,
    query: Query,
    device_id: str,
    pass_num: int,
    metrics: MetricsStore | None = None,
) -> PassResult:
    """Выполняет один проход проверки сайта на устройстве."""
    wait_seconds = int(getattr(site, "phone_wait_seconds", 40) or 40)
    wait_for = getattr(site, "phone_wait_for", "") or ""
    capture_json = getattr(site, "phone_capture_json", None) or []
    pass_timeout = wait_seconds + 30

    t0 = time.monotonic()
    extra = await resolve_extra(site, query)
    url = build_url(site, query, extra=extra)

    snap = None
    finished = False
    err_str = None
    pass_ok = False
    is_captcha = False
    offers = []
    min_price = None
    is_complete = True

    try:
        snap, finished = await asyncio.wait_for(
            fetch_page_snapshot(
                settings,
                device_id,
                url,
                wait_seconds=wait_seconds,
                capture_json=capture_json,
                wait_for=wait_for,
                priority=0,
                site_name=site.name,
            ),
            timeout=pass_timeout,
        )
    except asyncio.TimeoutError:
        err_str = "таймаут"
    except Exception as exc:
        exc_s = str(exc).lower()
        if "502" in exc_s or "bad gateway" in exc_s or "не отвечает" in exc_s or "offline" in exc_s or "офлайн" in exc_s:
            err_str = "502"
        else:
            err_str = str(exc) or "ошибка"

    elapsed = time.monotonic() - t0

    if snap is not None:
        if snap.blocked:
            is_captcha = True
            err_str = "капча"
        else:
            parser = PARSERS.get(site.parser)
            if parser is not None:
                try:
                    offers = parser(snap, query) or []
                except Exception as exc:
                    logger.debug("Ошибка парсера %s: %s", site.parser, exc)
                    offers = []

                checker = getattr(parser, "results_complete", None)
                if checker is None and hasattr(parser, "__module__"):
                    import sys
                    mod = sys.modules.get(parser.__module__)
                    if mod:
                        checker = getattr(mod, "results_complete", None)
                if callable(checker):
                    try:
                        is_complete = bool(checker(snap))
                    except Exception:
                        is_complete = True
                else:
                    is_complete = True

                offers, _ = filter_wrong_dates(offers, query)
                if offers:
                    min_price = min(o.price for o in offers)
                    pass_ok = True
                else:
                    err_str = "пусто"
            else:
                err_str = "нет парсера"

    # Запись замера в метрики site_runs
    if metrics is not None:
        try:
            metrics.record_run(
                source="phone_check",
                site=site.name,
                ok=pass_ok,
                partial=not is_complete,
                captcha=is_captcha,
                error=err_str,
                offers=len(offers),
                elapsed=elapsed,
                device=device_id,
                fetch_seconds=elapsed,
                priority=0,
                min_price=min_price,
            )
        except Exception as e:
            logger.debug("Ошибка записи метрики phone_check: %s", e)

    return PassResult(
        pass_num=pass_num,
        ok=pass_ok,
        elapsed=elapsed,
        captcha=is_captcha,
        offers_count=len(offers),
        min_price=min_price,
        results_complete=is_complete,
        error=err_str,
    )


def evaluate_site(
    site_name: str,
    passes: list[PassResult],
    ref_price: float | None = None,
) -> SiteCheckResult:
    """Вычисляет вердикт по сайту на основе двух проходов."""
    has_ok_pass = False
    price_note = ""
    if ref_price is None:
        price_note = "эталон отсутствует, сверка пропущена"

    for p in passes:
        if not p.ok or p.captcha or p.offers_count == 0:
            continue
        if not (p.results_complete or p.offers_count >= 5):
            continue
        if ref_price is not None and p.min_price is not None:
            diff = abs(p.min_price - ref_price) / ref_price
            if diff > 0.15:
                continue
        has_ok_pass = True
        break

    if has_ok_pass:
        return SiteCheckResult(
            site_name=site_name,
            passes=passes,
            verdict="ок",
            reason="",
            ref_price=ref_price,
            price_note=price_note,
        )

    # Причины «не ок»: 502, капча, пусто, неполно, цена расходится
    reason = "не ок"
    non_empty_errs = [p.error for p in passes if p.error and p.error not in ("пусто", "неполно")]
    if all(p.error == "502" for p in passes):
        reason = "502"
    elif any(p.captcha or p.error == "капча" for p in passes):
        reason = "капча"
    elif any(
        ref_price is not None
        and p.min_price is not None
        and abs(p.min_price - ref_price) / ref_price > 0.15
        for p in passes
    ):
        reason = "цена расходится"
    elif non_empty_errs:
        reason = non_empty_errs[0]
    elif any(not p.results_complete and 0 < p.offers_count < 5 for p in passes):
        reason = "неполно"
    elif all(p.offers_count == 0 for p in passes) or all(p.error == "пусто" for p in passes):
        reason = "пусто"
    else:
        errs = [p.error for p in passes if p.error]
        reason = errs[0] if errs else "не ок"

    return SiteCheckResult(
        site_name=site_name,
        passes=passes,
        verdict="не ок",
        reason=reason,
        ref_price=ref_price,
        price_note=price_note,
    )


def format_phone_check_report(
    phone_name: str,
    phone_id: str,
    site_results: dict[str, SiteCheckResult],
    unresponsive: bool,
    ok_sites: list[str],
    prev_status: str,
) -> str:
    """Форматирует сообщение-отчёт для администраторов."""
    lines = [f"📱 <b>Результаты проверки {escape(phone_name)}</b> (<code>{escape(phone_id)}</code>)\n"]
    if unresponsive:
        lines.append(f"⚠️ Телефон <b>{escape(phone_name)}</b> не отвечает, проверьте экран и питание.")
        lines.append("Проверка остановлена досрочно.")
        return "\n".join(lines)

    for s_name, res in site_results.items():
        verdict_badge = "✅ <b>ок</b>" if res.verdict == "ок" else f"❌ <b>не ок ({escape(res.reason)})</b>"
        lines.append(f"<b>{escape(s_name)}:</b>")
        for p in res.passes:
            if p.ok and p.min_price is not None:
                compl_str = "полная выдача" if p.results_complete else "неполная"
                lines.append(
                    f"• Проход {p.pass_num}: {p.elapsed:.1f} с, {p.min_price:,} ₽, {p.offers_count} офферов ({compl_str})".replace(",", " ")
                )
            elif p.captcha:
                lines.append(f"• Проход {p.pass_num}: {p.elapsed:.1f} с, капча")
            elif p.error:
                lines.append(f"• Проход {p.pass_num}: {p.elapsed:.1f} с, ошибка: {escape(p.error)}")
            else:
                lines.append(f"• Проход {p.pass_num}: {p.elapsed:.1f} с, 0 офферов")

        if res.price_note:
            lines.append(f"• Сверка цен: {escape(res.price_note)}")
        elif res.ref_price is not None:
            lines.append(f"• Эталон цены: {res.ref_price:,.0f} ₽".replace(",", " "))
        lines.append(f"• Вердикт: {verdict_badge}")
        lines.append("")

    if ok_sites:
        lines.append(f"<b>Итог:</b> допущен на: <b>{escape(', '.join(ok_sites))}</b>")
        if prev_status == "hold":
            lines.append("<i>(статус остался hold — включите вручную)</i>")
    else:
        reasons = [f"{s} ({site_results[s].reason})" for s in site_results]
        lines.append(f"<b>Итог:</b> <b>не допущен:</b> {escape(', '.join(reasons))}")
        if prev_status == "active":
            lines.append("<i>(активный телефон не выключен)</i>")

    return "\n".join(lines)


async def _do_run_phone_check(
    settings: Settings,
    phone_id: str,
    bot: Any = None,
    initiated_by_chat_id: int | None = None,
    metrics: MetricsStore | None = None,
) -> PhoneCheckReport:
    reg = getattr(settings, "phones", None)
    if reg is None:
        from .phones import PhoneRegistry
        reg = PhoneRegistry(settings.storage_dir)
        settings.phones = reg

    phone_entry = reg.get(phone_id) or {}
    phone_name = phone_entry.get("name") or phone_id
    prev_status = phone_entry.get("status", "new")
    existing_deny = list(phone_entry.get("sites_deny", []))

    # Контрольный маршрут
    ctrl_depart = date.today() + timedelta(days=DEFAULT_CONTROL_DAYS_AHEAD)
    query = Query(
        origin=DEFAULT_CONTROL_ORIGIN,
        destination=DEFAULT_CONTROL_DESTINATION,
        depart=ctrl_depart,
        adults=1,
    )

    metrics_store = metrics
    if metrics_store is None:
        if bot is not None and getattr(bot, "metrics", None) is not None:
            metrics_store = bot.metrics
        else:
            try:
                metrics_store = MetricsStore(Path(settings.storage_dir) / "metrics.sqlite")
            except Exception:
                metrics_store = None

    phone_sites = [s for s in settings.sites if getattr(s, "phone", None)]
    site_results: dict[str, SiteCheckResult] = {}
    unresponsive = False

    for s_idx, site in enumerate(phone_sites):
        passes: list[PassResult] = []
        for p_num in (1, 2):
            p_res = await run_site_pass(
                settings=settings,
                site=site,
                query=query,
                device_id=phone_id,
                pass_num=p_num,
                metrics=metrics_store,
            )
            passes.append(p_res)

        ref_price = get_reference_price(settings, site.name, query, exclude_device=phone_id)
        s_eval = evaluate_site(site.name, passes, ref_price=ref_price)
        site_results[site.name] = s_eval

        # Проверка на неотвечающий телефон: если все проходы 502 за короткое время
        if s_idx == 0 and all(p.error == "502" for p in passes):
            unresponsive = True
            break

    ok_sites = [r.site_name for r in site_results.values() if r.verdict == "ок"]
    failed_sites = [r.site_name for r in site_results.values() if r.verdict != "ок"]

    if unresponsive:
        note = "не отвечает, проверьте экран и питание"
        reg.set_note(phone_id, note)
    elif ok_sites:
        if prev_status == "new":
            reg.set_status(phone_id, "active")
            reg.set_sites(phone_id, allow=ok_sites, deny=existing_deny)
            reg.set_note(phone_id, f"Допущен на: {', '.join(ok_sites)}")
        elif prev_status == "hold":
            reg.set_sites(phone_id, allow=ok_sites, deny=existing_deny)
            reg.set_note(phone_id, f"Проверен (hold): {', '.join(ok_sites)}")
        else:  # active
            reg.set_sites(phone_id, allow=ok_sites, deny=existing_deny)
            reg.set_note(phone_id, f"Проверен: {', '.join(ok_sites)}")
    else:
        reason_parts = [f"{s} ({site_results[s].reason})" for s in site_results]
        note = f"Не допущен: {', '.join(reason_parts)}"
        reg.set_note(phone_id, note)

    report_text = format_phone_check_report(
        phone_name=phone_name,
        phone_id=phone_id,
        site_results=site_results,
        unresponsive=unresponsive,
        ok_sites=ok_sites,
        prev_status=prev_status,
    )

    if bot is not None:
        recipients = set(getattr(settings, "tg_allowed_ids", set()))
        if initiated_by_chat_id:
            recipients.add(initiated_by_chat_id)
        for chat_id in recipients:
            try:
                await bot.send(chat_id, report_text)
            except Exception as e:
                logger.warning("Ошибка отправки отчёта проверки чату %s: %s", chat_id, e)

    return PhoneCheckReport(
        phone_id=phone_id,
        phone_name=phone_name,
        site_results=site_results,
        ok_sites=ok_sites,
        failed_sites=failed_sites,
        unresponsive=unresponsive,
        admitted=bool(ok_sites and prev_status == "new"),
        message=report_text,
    )


async def run_phone_check(
    settings: Settings,
    phone_id: str,
    bot: Any = None,
    initiated_by_chat_id: int | None = None,
    metrics: MetricsStore | None = None,
) -> PhoneCheckReport:
    """Запускает проверку телефона в фоне с контролем повторного запуска."""
    clean_id = str(phone_id).strip()
    if is_checking(clean_id):
        logger.warning("Проверка телефона %s уже выполняется", clean_id)
        return PhoneCheckReport(
            phone_id=clean_id,
            phone_name=clean_id,
            message="Уже проверяется",
        )

    current_task = asyncio.current_task()
    if current_task is not None:
        _active_checks[clean_id] = current_task

    reg = getattr(settings, "phones", None)
    if reg is None:
        from .phones import PhoneRegistry
        reg = PhoneRegistry(settings.storage_dir)
        settings.phones = reg

    phone_entry = reg.get(clean_id) or {}
    phone_name = phone_entry.get("name") or clean_id

    reg.set_checking(clean_id, True)

    try:
        return await asyncio.wait_for(
            _do_run_phone_check(
                settings=settings,
                phone_id=clean_id,
                bot=bot,
                initiated_by_chat_id=initiated_by_chat_id,
                metrics=metrics,
            ),
            timeout=MAX_OVERALL_CHECK_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Превышен общий таймаут проверки телефона %s (15 мин)", clean_id)
        note = "Не допущен: превышен общий лимит времени (15 мин)"
        reg.set_note(clean_id, note)
        report_text = (
            f"📱 <b>Результаты проверки {escape(phone_name)}</b> (<code>{escape(clean_id)}</code>)\n\n"
            f"❌ <b>Ошибка:</b> превышен общий лимит времени проверки (15 минут).\n"
            f"<b>Итог:</b> <b>не допущен:</b> таймаут"
        )
        if bot is not None:
            recipients = set(getattr(settings, "tg_allowed_ids", set()))
            if initiated_by_chat_id:
                recipients.add(initiated_by_chat_id)
            for chat_id in recipients:
                try:
                    await bot.send(chat_id, report_text)
                except Exception as e:
                    logger.warning("Ошибка отправки отчёта проверки чату %s: %s", chat_id, e)
        return PhoneCheckReport(
            phone_id=clean_id,
            phone_name=phone_name,
            message=report_text,
        )
    except Exception as exc:
        logger.exception("Исключение при проверке телефона %s: %s", clean_id, exc)
        note = f"Не допущен: ошибка проверки ({exc})"
        reg.set_note(clean_id, note)
        report_text = (
            f"📱 <b>Результаты проверки {escape(phone_name)}</b> (<code>{escape(clean_id)}</code>)\n\n"
            f"❌ <b>Ошибка при проверке:</b> {escape(str(exc))}\n"
            f"<b>Итог:</b> <b>не допущен:</b> ошибка"
        )
        if bot is not None:
            recipients = set(getattr(settings, "tg_allowed_ids", set()))
            if initiated_by_chat_id:
                recipients.add(initiated_by_chat_id)
            for chat_id in recipients:
                try:
                    await bot.send(chat_id, report_text)
                except Exception as e:
                    logger.warning("Ошибка отправки отчёта проверки чату %s: %s", chat_id, e)
        return PhoneCheckReport(
            phone_id=clean_id,
            phone_name=phone_name,
            message=report_text,
        )
    finally:
        reg.set_checking(clean_id, False)
        set_checking(clean_id, False)
