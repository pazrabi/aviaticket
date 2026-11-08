from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Awaitable, Callable
import uuid

from .collector import fetch_snapshot, Snapshot
from .config import Settings, SiteConfig
from .dispatcher import check_site_fuse_async, _get_metrics, _FUSE_STATE
from .droidhub import (
    DroidhubError,
    PreemptedError,
    PreferenceList,
    candidates,
    fetch_page_snapshot_any,
    phone_enabled,
)
from .extract import extract_offers
from .fresh_cache import FreshCache
from .guard import SearchGuard, friendly_error
from .metrics import run_metrics_io, submit_metrics_write
from .models import Offer, Query, SiteResult
from .parsers import PARSERS
from .parsers.kupibilet import build_ticket_url
from .parsers.travelpayouts import fetch_travelpayouts, search_url
from .sites import build_url
from .statedb import db_path
from .tutu_ids import resolve_city
from . import yandex_ids
from .yandex_ids import city_id

log = logging.getLogger("avia.pipeline")

# Общий SearchGuard на процесс (модульный объект для тестов с подменой)
guard = SearchGuard()

# Пауза перед повторной попыткой при 0 вариантов (ParserEmptyError)
EMPTY_RETRY_DELAY: float = 3.0

# Минимальный интервал между быстрыми разборами в evaluate (сек)
PARSER_MIN_INTERVAL: float = 1.0

# Имя отладочного снимка пустой выдачи: {сайт}_{tag}_{ГГГГММДД_ЧЧММСС}.json (см. _save_empty_snapshot)
DEBUG_SNAPSHOT_NAME = re.compile(r"[\w.-]+_(?:empty|wrongdates|short)_\d{8}_\d{6}\.json")
# Время билета: ЧЧ:ММ; дата-время: ГГГГ-ММ-ДДTЧЧ:ММ (дальше могут идти секунды и пояс)
_CLOCK_TIME = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d")
_ISO_MINUTE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d")
# Сколько символов короткой страницы показать в ошибке «сайт не отдал выдачу»
SHORT_PAGE_EXCERPT_CHARS = 60


def _attempt_role(role: str | None, attempt_tracker: list[int] | None) -> str:
    """Роль попытки: hedge как есть, иначе primary для первой попытки сайта и retry для следующих."""
    eff_role = role or "primary"
    if eff_role != "hedge":
        eff_role = "primary"
        if attempt_tracker is not None:
            if attempt_tracker[0] > 0:
                eff_role = "retry"
            attempt_tracker[0] += 1
    return eff_role


def _record_attempt_nowait(
    settings: Settings,
    site_name: str,
    device: str | None,
    priority: int,
    outcome: str,
    offers: int,
    fetch_seconds: float | None,
    queue_wait: float | None,
    search_id: str | None,
    attempt_tracker: list[int] | None = None,
    stop_reason: str | None = None,
    t_first_offer: float | None = None,
    t_last_improve: float | None = None,
    role: str | None = None,
) -> None:
    """Попытка из _from_browser: роль, время и база — сразу, в цикле событий; запись — без ожидания.

    Готовые цены сайта не ждут очереди к SQLite и не теряются, если поиск отменят, пока запись
    стоит в очереди. Ошибку записи пишет в лог поток записи метрик.
    """
    eff_role = _attempt_role(role, attempt_tracker)
    metrics = _get_metrics(settings)
    if metrics is None:
        return
    source = "user" if priority == 0 else ("sub" if priority == 1 else "bg")
    submit_metrics_write(
        metrics.record_attempt,
        site=site_name,
        device=device,
        source=source,
        role=eff_role,
        outcome=outcome,
        offers=offers,
        fetch_seconds=fetch_seconds,
        queue_wait=queue_wait,
        search_id=search_id,
        ts=time.time(),
        stop_reason=stop_reason,
        t_first_offer=t_first_offer,
        t_last_improve=t_last_improve,
    )


def _hit_time_cap(priority: int, cur_stats: dict[str, Any], wait_seconds: Any) -> bool:
    """Живой поиск упёрся в лимит ожидания страницы и ничего не получил.

    Другой телефон будет ждать тот же срок с тем же итогом, поэтому живой запрос
    не гоняем по кругу: он вернёт то, что есть, а не удвоит время ответа.
    """
    if priority != 0:
        return False
    if cur_stats.get("finished") is True:
        # страница дозагрузилась, но разбор дал 0: это про телефон/вёрстку, повтор на другом телефоне разумен
        return False
    try:
        fetched = float(cur_stats.get("fetch_seconds") or 0.0)
        cap = float(wait_seconds or 0.0)
    except (TypeError, ValueError):
        return False
    return cap > 0 and fetched >= cap * 0.8


def _can_retry_on_phone(
    priority: int,
    cur_stats: dict[str, Any],
    wait_seconds: Any,
    remaining_cands: Sequence[str] | None,
    settings: Settings,
    site_name: str,
    search_started: float | None = None,
    now: float | None = None,
    deep: bool = False,
) -> bool:
    """Проверяет, разрешён ли повтор на следующем телефоне для живого поиска.

    Повтор на следующем телефоне разрешён, если «сколько уже прошло с начала поиска +
    ожидаемое время (медиана) этой пары сайт/телефон следующего кандидата» <= live_safety_seconds (или deep_safety_seconds при deep=True).
    Если search_started is None, используется старое поведение _hit_time_cap.
    Фон и подписки (priority != 0) не меняются (всегда разрешён повтор при наличии кандидатов).
    """
    if priority != 0:
        return not _hit_time_cap(priority, cur_stats, wait_seconds)

    if search_started is None:
        return not _hit_time_cap(priority, cur_stats, wait_seconds)

    if not remaining_cands:
        return False

    cap_attr = "deep_safety_seconds" if deep else "live_safety_seconds"
    default_cap = 150.0 if deep else 90.0
    cap_val = getattr(settings, cap_attr, default_cap)
    safety_cap = float(cap_val) if cap_val is not None else default_cap
    if safety_cap <= 0:
        return True

    next_phone = remaining_cands[0]

    expected_sec: float | None = None
    try:
        from .speed import get_speed_table

        table = get_speed_table(settings)
        st = table.get_stats(site_name, next_phone, "live", now=now)
        exp = st.cost if st.cost is not None else st.median_fetch
        if exp is not None and exp > 0:
            expected_sec = float(exp)
    except Exception:
        expected_sec = None

    if expected_sec is None:
        wake = float(getattr(settings, "phone_wake_seconds", 5.0) or 5.0)
        try:
            site_wait = float(wait_seconds or 0.0)
        except (TypeError, ValueError):
            site_wait = 0.0
        expected_sec = wake + site_wait

    current_ts = time.monotonic() if now is None else now
    elapsed = max(0.0, current_ts - search_started)

    return (elapsed + expected_sec) <= safety_cap


_can_retry = _can_retry_on_phone
can_retry_on_phone = _can_retry_on_phone


class ParserEmptyError(Exception):
    """Исключение при 0 вариантов от парсера на непустой странице."""

    def __init__(
        self,
        message: str,
        page_url: str | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.page_url = page_url
        self.snapshot_path = snapshot_path


def _save_empty_snapshot(
    site: SiteConfig,
    snap: Snapshot,
    settings: Settings,
    tag: str = "empty",
) -> Path | None:
    """Сохраняет отладочный снимок при 0 вариантов или неверных датах (без токенов)."""
    try:
        debug_dir = Path(settings.debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = debug_dir / f"{site.name}_{tag}_{ts}.json"

        data = {
            "url": snap.url,
            "text": snap.text,
            "json_payloads": snap.json_payloads,
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if settings.tp_token:
            content = content.replace(settings.tp_token, "[REDACTED]")
        if settings.tg_token:
            content = content.replace(settings.tg_token, "[REDACTED]")
        if settings.droidhub_token:
            content = content.replace(settings.droidhub_token, "[REDACTED]")
        content = re.sub(
            r'([?&](?:token|access_token|api_key|key|secret)=)[^&"\s]+',
            r'\1[REDACTED]',
            content,
            flags=re.I,
        )
        target.write_text(content, encoding="utf-8")
        _prune_debug_snapshots(debug_dir, int(getattr(settings, "debug_snapshots_max", 100) or 0))
        return target
    except Exception as e:
        log.warning("Не удалось сохранить отладочный снимок для %s: %s", site.name, e)
        return None


def _drop_debug_snapshot(exc: ParserEmptyError) -> None:
    """Удаляет отладочный снимок попытки, за которой последует повтор."""
    if exc.snapshot_path and exc.snapshot_path.exists():
        try:
            exc.snapshot_path.unlink()
        except OSError:
            pass


def _prune_debug_snapshots(debug_dir: Path, keep: int) -> None:
    """Оставляет в debug_dir не больше keep снимков пустой выдачи, удаляя самые старые.

    Трогает только файлы с именем снимка (DEBUG_SNAPSHOT_NAME) в самом debug_dir, без рекурсии;
    keep <= 0 — без предела.
    """
    if keep <= 0:
        return
    try:
        snaps = [
            p for p in debug_dir.iterdir()
            if DEBUG_SNAPSHOT_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        ]
        snaps.sort(key=lambda p: (p.stat().st_mtime, p.name))
        for old in snaps[:-keep]:
            old.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Не удалось почистить отладочные снимки в %s: %s", debug_dir, e)


def filter_wrong_dates(offers: list[Offer], q: Query) -> tuple[list[Offer], int]:
    """Оставляет только офферы с датами вылета, соответствующими запросу.

    - offer.depart_at (первые 10 символов, ISO YYYY-MM-DD) == q.depart.isoformat()
    - если q.return_date задан и у оффера есть back с back.depart_at:
      back.depart_at[:10] == q.return_date.isoformat()
    Если depart_at или back.depart_at пусты (None) — оффер не отбрасывается.
    Возвращает (оставшиеся офферы, число отброшенных).
    """
    kept: list[Offer] = []
    dropped = 0
    depart_iso = q.depart.isoformat()
    return_iso = q.return_date.isoformat() if q.return_date else None

    for offer in offers:
        if offer.depart_at and offer.depart_at[:10] != depart_iso:
            dropped += 1
            continue

        if (
            return_iso
            and offer.back
            and offer.back.depart_at
            and offer.back.depart_at[:10] != return_iso
        ):
            dropped += 1
            continue

        kept.append(offer)

    return kept, dropped


def _has_bad_time(offer: Offer) -> bool:
    """Время в билете есть, но не ЧЧ:ММ (дата-время — не ГГГГ-ММ-ДДTЧЧ:ММ): элемент выдачи кривой.

    Пустое время или его отсутствие — время неизвестно, билет остаётся. Проверяются оба плеча.
    """
    back = getattr(offer, "back", None)
    for leg in (offer, back) if back is not None else (offer,):
        for value in (leg.depart_time, leg.arrive_time):
            if value and not (isinstance(value, str) and _CLOCK_TIME.fullmatch(value)):
                return True
        for value in (leg.depart_at, leg.arrive_at):
            if value and not (isinstance(value, str) and _ISO_MINUTE.match(value)):
                return True
    return False


def _has_json_answer(snap: Snapshot) -> bool:
    """JSON-ответ — перехваченный ответ сайта (snap.json_payloads), тело которого разбирается как JSON."""
    for _url, body in snap.json_payloads or []:
        try:
            json.loads(body)
        except (TypeError, ValueError):
            continue
        return True
    return False


def _page_excerpt(text: str) -> str:
    return " ".join(text.split())[:SHORT_PAGE_EXCERPT_CHARS] or "пустая страница"


async def _from_api(site: SiteConfig, q: Query, settings: Settings) -> tuple[list[Offer], str | None]:
    if site.parser == "travelpayouts":
        offers = await fetch_travelpayouts(q, settings.tp_token, settings.tp_marker)
        return offers, search_url(q, settings.tp_marker)
    raise RuntimeError(f"неизвестный api-источник: {site.parser!r}")


async def resolve_extra(site: SiteConfig, q: Query) -> dict[str, str] | None:
    """Доп. подстановки для url_template (id городов у tutu/yandex)."""
    extra: dict[str, str] | None = None
    if site.id_resolver == "tutu":
        origin_id, origin_seo = await resolve_city(q.origin)
        dest_id, dest_seo = await resolve_city(q.destination)
        extra = {
            "origin_id": origin_id,
            "destination_id": dest_id,
            "origin_seo": origin_seo,
            "destination_seo": dest_seo,
        }
    elif site.id_resolver == "yandex":
        extra = {
            "origin_id": await yandex_ids.resolve(q.origin),
            "destination_id": await yandex_ids.resolve(q.destination),
        }
    return extra


async def _process_snapshot(
    site: SiteConfig,
    snap: Snapshot,
    q: Query,
    settings: Settings,
    *,
    partial: bool = False,
) -> tuple[list[Offer], str | None, bool]:
    """Разбор полученного снимка страницы: парсер, результаты, даты и сортировка."""
    offers: list[Offer] = []
    parser = PARSERS.get(site.parser)
    if site.parser and parser is None:
        raise RuntimeError(f"парсер {site.parser!r} не зарегистрирован")
    if parser:
        timeout = float(getattr(settings, "parser_timeout_seconds", 5.0) or 0)
        if timeout > 0:
            try:
                if inspect.iscoroutinefunction(parser):
                    offers = await asyncio.wait_for(parser(snap, q), timeout=timeout)
                else:
                    offers = await asyncio.wait_for(asyncio.to_thread(parser, snap, q), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise TimeoutError("разбор не уложился в таймаут") from exc
        else:
            if inspect.iscoroutinefunction(parser):
                offers = await parser(snap, q)
            else:
                offers = parser(snap, q)
        total = getattr(offers, "total", 0) or len(offers)
        good_time = [o for o in offers if not _has_bad_time(o)]
        skipped = getattr(offers, "skipped", 0) + len(offers) - len(good_time)
        offers = good_time
        if skipped:
            log.warning("Сайт %s: пропущено %d из %d элементов выдачи", site.name, skipped, total)
        for o in offers:
            o.site = site.name
        checker = getattr(parser, "results_complete", None)
        if checker is None and hasattr(parser, "__module__"):
            import sys
            mod = sys.modules.get(parser.__module__)
            if mod:
                checker = getattr(mod, "results_complete", None)
        if callable(checker):
            try:
                if not checker(snap):
                    partial = True
            except Exception:
                pass
    if not offers and site.use_llm:
        offers = await extract_offers(snap, q, site.name, settings)
    if not offers and not parser and not site.use_llm:
        raise RuntimeError("для сайта нет парсера: запустите `python -m avia.recon` и пришлите debug/recon")

    # Проверка: 0 вариантов при непустой странице (> 200 символов) и не капча
    if not offers and parser:
        text_stripped = snap.text.strip()
        is_captcha = snap.blocked
        if len(text_stripped) > 200 and not is_captcha:
            snap_path = _save_empty_snapshot(site, snap, settings)
            raise ParserEmptyError(
                "формат выдачи мог измениться, 0 вариантов при непустой странице",
                page_url=snap.url,
                snapshot_path=snap_path,
            )
        # SHORT_PAGE_FAIL: короткая страница (не длиннее 200 символов) без JSON-ответов — сайт не отдал выдачу
        if getattr(settings, "short_page_fail", False) and not is_captcha and not _has_json_answer(snap):
            snap_path = _save_empty_snapshot(site, snap, settings, tag="short")
            raise ParserEmptyError(
                f"сайт не отдал выдачу: «{_page_excerpt(text_stripped)}»",
                page_url=snap.url,
                snapshot_path=snap_path,
            )

    had_offers = bool(offers)
    offers, dropped = filter_wrong_dates(offers, q)
    if dropped > 0:
        log.warning("Сайт %s: отброшено %d офферов с неверными датами", site.name, dropped)
    if not offers and had_offers:
        snap_path = _save_empty_snapshot(site, snap, settings, tag="wrongdates")
        raise ParserEmptyError(
            "сайт вернул билеты на другие даты, а не на запрошенные",
            page_url=snap.url,
            snapshot_path=snap_path,
        )

    for o in offers:
        if (
            not o.url
            and (o.site == "kupibilet" or getattr(site, "name", None) == "kupibilet")
            and getattr(o, "ticket_hash", None)
            and getattr(settings, "kupibilet_ticket_link", True)
            and snap.url
        ):
            try:
                o.url = build_ticket_url(snap.url, o.ticket_hash)
            except Exception as e:
                log.warning("Ошибка построения ссылки на билет Купибилет: %s", e)
        o.url_is_direct = bool(o.url and o.url != snap.url)
        o.url = o.url or snap.url
    return sorted(offers, key=lambda o: o.price), snap.url, partial


def _quick_parse_offers(site: SiteConfig, snap: Snapshot, q: Query) -> list[Offer]:
    """Быстрый синхронный разбор офферов парсером сайта для evaluate."""
    parser = PARSERS.get(site.parser) if site and site.parser else None
    if parser is None:
        return []
    if inspect.iscoroutinefunction(parser):
        offers = asyncio.run(parser(snap, q))
    else:
        offers = parser(snap, q)
    if not offers or not isinstance(offers, list):
        return []
    offers = [o for o in offers if not _has_bad_time(o)]
    offers, _ = filter_wrong_dates(offers, q)
    return offers


_timeline_tasks: set[asyncio.Task] = set()


async def _record_timeline_bg(
    settings: Settings,
    site: SiteConfig,
    snap: Snapshot,
    q: Query,
    search_id: str | None,
    device: str | None,
) -> None:
    try:
        from .price_timeline import build_timeline
        timeline = await build_timeline(site, snap, q, settings)
        if timeline:
            metrics = getattr(settings, "metrics", None)
            if metrics is None:
                storage_dir = getattr(settings, "storage_dir", None)
                if storage_dir is not None:
                    from .metrics import MetricsStore
                    metrics = MetricsStore(db_path(storage_dir, "metrics"))
            if metrics is not None:
                metrics.record_timeline(
                    search_id=search_id,
                    site=site.name,
                    device=device,
                    **timeline,
                )
    except Exception as exc:
        log.warning("Ошибка при сохранении price_timeline для %s: %s", site.name, exc)


async def _from_browser(
    site: SiteConfig,
    q: Query,
    settings: Settings,
    *,
    priority: int = 0,
    weight: int = 0,
    stats: dict | None = None,
    exclude_devices: set[str] | None = None,
    device_order: list[str] | None = None,
    search_id: str | None = None,
    attempt_tracker: list[int] | None = None,
    search_started: float | None = None,
    now: float | None = None,
    deep: bool = False,
    target_device: str | None = None,
    already_acquired: bool = False,
    role: str = "primary",
) -> tuple[list[Offer], str | None, bool]:
    if attempt_tracker is None:
        attempt_tracker = [0]
    extra = await resolve_extra(site, q)
    url = build_url(site, q, extra=extra)

    if (priority > 0 or deep) and getattr(site, "phone_full_wait_seconds", None) is not None:
        wait_seconds = site.phone_full_wait_seconds
    else:
        wait_seconds = getattr(site, "phone_wait_seconds", 40)

    if (priority > 0 or deep) and getattr(site, "phone_full_wait_for", None) is not None:
        wait_for = site.phone_full_wait_for
    else:
        wait_for = getattr(site, "phone_wait_for", "")

    effective_weight = weight if weight > 0 else int(wait_seconds or 0)
    patience = getattr(site, "phone_patience_seconds", 0) or 0

    if phone_enabled(settings, site):
        cand_list = candidates(settings, site, priority=priority)
        orig_patience = getattr(cand_list, "patience", patience)
        orig_preferred_count = getattr(cand_list, "preferred_count", None)
        pref_base_set = (
            set(cand_list[:orig_preferred_count])
            if (isinstance(cand_list, PreferenceList) and orig_preferred_count is not None)
            else None
        )

        remaining_cands = list(cand_list)
        if priority == 0 and getattr(settings, "speed_routing", True):
            # пары в кулдауне после капчи не трогаем: каждая попытка только плодит капчи и тратит время человека
            try:
                from .speed import get_speed_table

                st_table = get_speed_table(settings)
                ok_cands = [
                    c
                    for c in remaining_cands
                    if not (
                        st_table.get_pair_status(site.name, c).in_quarantine
                        and st_table.get_pair_status(site.name, c).consecutive_captchas > 0
                    )
                ]
            except Exception:
                ok_cands = remaining_cands
            if remaining_cands and not ok_cands:
                raise DroidhubError("сайт показал капчу на всех телефонах, владельцу нужно пройти её руками")
            remaining_cands = ok_cands
        if exclude_devices:
            # повтор после пустой выдачи: не возвращаемся на телефон, который её отдал
            filtered = [c for c in remaining_cands if c not in exclude_devices]
            if filtered:
                remaining_cands = filtered
        if priority == 0 and device_order:
            order_seen: set[str] = set()
            ordered_part: list[str] = []
            for d in device_order:
                if d in remaining_cands and d not in order_seen:
                    order_seen.add(d)
                    ordered_part.append(d)
            rest_part = [d for d in remaining_cands if d not in order_seen]
            remaining_cands = ordered_part + rest_part
        if target_device:
            remaining_cands = [target_device]
        last_snap = None
        last_finished = True
        last_exc: Exception | None = None

        while remaining_cands:
            cur_stats: dict[str, Any] = {}
            if isinstance(cand_list, PreferenceList):
                if pref_base_set is not None:
                    rem_pref = sum(1 for c in remaining_cands if c in pref_base_set)
                    cur_patience = orig_patience if rem_pref > 0 else 0.0
                    cur_pref_count = max(1, rem_pref) if rem_pref > 0 else len(remaining_cands)
                else:
                    cur_patience = orig_patience
                    cur_pref_count = orig_preferred_count
                pref_arg = PreferenceList(remaining_cands, patience=cur_patience, preferred_count=cur_pref_count)
            else:
                cur_patience = orig_patience
                pref_arg = remaining_cands

            eval_cb = None
            if not deep and priority == 0 and getattr(settings, "fast_stop", True):
                from . import stop_rule
                stable_sec = stop_rule.stable_seconds_for(site.name, settings)
                cap_sec = float(wait_seconds or 40.0)
                stop_params = stop_rule.StopParams(stable_seconds=stable_sec, cap_seconds=cap_sec)

                best_min: float | None = None
                n_offers: int = 0
                t_first_offer: float | None = None
                t_last_improve: float | None = None
                last_parse_time: float = 0.0
                last_parsed_count: int = 0

                async def _evaluate_cb(snap_so_far: Snapshot, hub_state: dict[str, Any]) -> str | None:
                    nonlocal best_min, n_offers, t_first_offer, t_last_improve
                    nonlocal last_parse_time, last_parsed_count

                    now = time.monotonic()
                    curr_count = len(snap_so_far.json_payloads)
                    has_new = curr_count > last_parsed_count
                    hub_st = hub_state.get("state")
                    should_parse = has_new and (
                        now - last_parse_time >= PARSER_MIN_INTERVAL or last_parse_time == 0.0 or hub_st in ("done", "failed")
                    )

                    if should_parse:
                        last_parse_time = now
                        last_parsed_count = curr_count
                        parsed_offers: list[Offer] = []
                        try:
                            parsed_offers = await asyncio.wait_for(
                                asyncio.to_thread(_quick_parse_offers, site, snap_so_far, q),
                                timeout=5.0,
                            )
                        except (asyncio.TimeoutError, TimeoutError, ParserEmptyError):
                            parsed_offers = []
                        except Exception as e:
                            log.warning("Ошибка быстрого разбора для %s: %s", site.name, e)
                            parsed_offers = []

                        prices = [float(o.price) for o in parsed_offers if getattr(o, "price", None) is not None]
                        if prices:
                            cur_min = min(prices)
                            valid_ts = [t for t in snap_so_far.payload_times if t is not None]
                            latest_t = valid_ts[-1] if valid_ts else float(hub_state.get("elapsed") or 0.0)

                            if best_min is None:
                                best_min = cur_min
                                n_offers = len(prices)
                                t_first_offer = latest_t
                                t_last_improve = latest_t
                            elif cur_min < best_min:
                                best_min = cur_min
                                n_offers = len(prices)
                                t_last_improve = latest_t
                            else:
                                n_offers = max(n_offers, len(prices))

                    _evaluate_cb.t_first_offer = t_first_offer
                    _evaluate_cb.t_last_improve = t_last_improve

                    st = {
                        "elapsed": float(hub_state.get("elapsed") or 0.0),
                        "wait_for_seen": bool(hub_state.get("wait_for_seen", False)),
                        "network_quiet": bool(hub_state.get("network_quiet", False)),
                        "captcha": bool(hub_state.get("captcha", False) or snap_so_far.blocked),
                        "best_min": best_min,
                        "n_offers": n_offers,
                        "t_last_improve": t_last_improve,
                        "job_state": hub_state.get("state") or "running",
                    }
                    return stop_rule.decide(st, stop_params)

                _evaluate_cb.t_first_offer = None
                _evaluate_cb.t_last_improve = None
                eval_cb = _evaluate_cb

            try:
                fetch_kwargs: dict[str, Any] = {
                    "wait_seconds": wait_seconds,
                    "capture_json": site.phone_capture_json,
                    "wait_for": wait_for,
                    "priority": priority,
                    "weight": effective_weight,
                    "stats": cur_stats,
                    "patience": cur_patience,
                    "site_name": site.name,
                    "max_seconds": wait_seconds,
                    "already_acquired": already_acquired,
                }
                if deep and getattr(site, "replay_post", None) is not None:
                    fetch_kwargs["replay_post"] = site.replay_post
                if eval_cb is not None:
                    fetch_kwargs["evaluate"] = eval_cb

                snap, finished = await fetch_page_snapshot_any(
                    settings,
                    pref_arg,
                    url,
                    **fetch_kwargs,
                )
            except PreemptedError:
                raise
            except Exception as exc:
                last_exc = exc
                if stats is not None:
                    stats.update(cur_stats)
                dev = cur_stats.get("device")
                outcome = "502" if "502" in str(exc) else (
                    "timeout" if ("таймаут" in str(exc).lower() or isinstance(exc, (asyncio.TimeoutError, TimeoutError))) else (
                        "captcha" if ("капч" in str(exc).lower() or "блокировк" in str(exc).lower()) else "error"
                    )
                )
                _record_attempt_nowait(
                    settings=settings,
                    site_name=site.name,
                    device=dev,
                    priority=priority,
                    outcome=outcome,
                    offers=0,
                    fetch_seconds=cur_stats.get("fetch_seconds"),
                    queue_wait=cur_stats.get("queue_wait"),
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    stop_reason=cur_stats.get("stop_reason"),
                    t_first_offer=cur_stats.get("t_first_offer"),
                    t_last_improve=cur_stats.get("t_last_improve"),
                    role=role,
                )
                raise

            cur_stats["finished"] = bool(finished)
            if stats is not None:
                stats.update(cur_stats)

            tried_devs = cur_stats.get("tried") or ([cur_stats["device"]] if cur_stats.get("device") else [])
            for td in tried_devs:
                if td in remaining_cands:
                    remaining_cands.remove(td)
            used_dev = cur_stats.get("device")
            if used_dev and used_dev in remaining_cands:
                remaining_cands.remove(used_dev)
            if not tried_devs and not used_dev and remaining_cands:
                remaining_cands.pop(0)

            if snap.blocked:
                _record_attempt_nowait(
                    settings=settings,
                    site_name=site.name,
                    device=used_dev,
                    priority=priority,
                    outcome="captcha",
                    offers=0,
                    fetch_seconds=cur_stats.get("fetch_seconds"),
                    queue_wait=cur_stats.get("queue_wait"),
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    stop_reason=cur_stats.get("stop_reason"),
                    t_first_offer=cur_stats.get("t_first_offer"),
                    t_last_improve=cur_stats.get("t_last_improve"),
                    role=role,
                )
                if remaining_cands:
                    try:
                        from .droidhub import _record_failure
                        _record_failure(
                            settings=settings,
                            site=site.name,
                            device=used_dev or "unknown",
                            priority=priority,
                            error="сайт показал капчу на телефоне",
                            captcha=True,
                            fetch_seconds=cur_stats.get("fetch_seconds"),
                            queue_wait=cur_stats.get("queue_wait"),
                        )
                    except Exception:
                        pass
                    log.warning("Сайт %s: капча на телефоне %s, пробуем следующий телефон", site.name, used_dev)
                    continue
                raise RuntimeError("сайт показал капчу на телефоне")

            partial = not finished
            stop_reason = cur_stats.get("stop_reason")
            if stop_reason in ("stable", "quiet", "cap"):
                partial = True
            try:
                offers, page_url, partial = await _process_snapshot(site, snap, q, settings, partial=partial)
            except ParserEmptyError as exc:
                exc_str = str(exc).lower()
                outcome = "wrong_dates" if ("другие даты" in exc_str or getattr(exc, "tag", None) == "wrongdates") else "empty"
                _record_attempt_nowait(
                    settings=settings,
                    site_name=site.name,
                    device=used_dev,
                    priority=priority,
                    outcome=outcome,
                    offers=0,
                    fetch_seconds=cur_stats.get("fetch_seconds"),
                    queue_wait=cur_stats.get("queue_wait"),
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    stop_reason=cur_stats.get("stop_reason"),
                    t_first_offer=cur_stats.get("t_first_offer"),
                    t_last_improve=cur_stats.get("t_last_improve"),
                    role=role,
                )
                if remaining_cands and _can_retry_on_phone(
                    priority,
                    cur_stats,
                    wait_seconds,
                    remaining_cands,
                    settings,
                    site.name,
                    search_started=search_started,
                    now=now,
                    deep=deep,
                ):
                    try:
                        from .droidhub import _record_failure
                        _record_failure(
                            settings=settings,
                            site=site.name,
                            device=used_dev or "unknown",
                            priority=priority,
                            error="неполная выдача без офферов",
                            partial=True,
                            offers=0,
                            fetch_seconds=cur_stats.get("fetch_seconds"),
                            queue_wait=cur_stats.get("queue_wait"),
                        )
                    except Exception:
                        pass
                    if exc.snapshot_path and exc.snapshot_path.exists():
                        try:
                            exc.snapshot_path.unlink()
                        except OSError:
                            pass
                    log.warning(
                        "Сайт %s: неполная/пустая выдача без офферов на телефоне %s (%s), пробуем следующий телефон",
                        site.name,
                        used_dev,
                        exc,
                    )
                    continue
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                _record_attempt_nowait(
                    settings=settings,
                    site_name=site.name,
                    device=used_dev,
                    priority=priority,
                    outcome="timeout",
                    offers=0,
                    fetch_seconds=cur_stats.get("fetch_seconds"),
                    queue_wait=cur_stats.get("queue_wait"),
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    stop_reason=cur_stats.get("stop_reason"),
                    t_first_offer=cur_stats.get("t_first_offer"),
                    t_last_improve=cur_stats.get("t_last_improve"),
                    role=role,
                )
                raise
            except Exception as exc:
                exc_str = str(exc).lower()
                outcome = "captcha" if ("капч" in exc_str or "блокировк" in exc_str) else "error"
                _record_attempt_nowait(
                    settings=settings,
                    site_name=site.name,
                    device=used_dev,
                    priority=priority,
                    outcome=outcome,
                    offers=0,
                    fetch_seconds=cur_stats.get("fetch_seconds"),
                    queue_wait=cur_stats.get("queue_wait"),
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    stop_reason=cur_stats.get("stop_reason"),
                    t_first_offer=cur_stats.get("t_first_offer"),
                    t_last_improve=cur_stats.get("t_last_improve"),
                    role=role,
                )
                raise

            # Проверка: неполная выдача без офферов
            if partial and not offers:
                _record_attempt_nowait(
                    settings=settings,
                    site_name=site.name,
                    device=used_dev,
                    priority=priority,
                    outcome="empty",
                    offers=0,
                    fetch_seconds=cur_stats.get("fetch_seconds"),
                    queue_wait=cur_stats.get("queue_wait"),
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    stop_reason=cur_stats.get("stop_reason"),
                    t_first_offer=cur_stats.get("t_first_offer"),
                    t_last_improve=cur_stats.get("t_last_improve"),
                    role=role,
                )
                if remaining_cands and _can_retry_on_phone(
                    priority,
                    cur_stats,
                    wait_seconds,
                    remaining_cands,
                    settings,
                    site.name,
                    search_started=search_started,
                    now=now,
                    deep=deep,
                ):
                    try:
                        from .droidhub import _record_failure
                        _record_failure(
                            settings=settings,
                            site=site.name,
                            device=used_dev or "unknown",
                            priority=priority,
                            error="неполная выдача без офферов",
                            partial=True,
                            offers=0,
                            fetch_seconds=cur_stats.get("fetch_seconds"),
                            queue_wait=cur_stats.get("queue_wait"),
                        )
                    except Exception:
                        pass
                    log.warning("Сайт %s: неполная выдача без офферов на телефоне %s, пробуем следующий телефон", site.name, used_dev)
                    continue
                return offers, page_url, partial

            outcome = "partial" if partial else "ok"
            _record_attempt_nowait(
                settings=settings,
                site_name=site.name,
                device=used_dev,
                priority=priority,
                outcome=outcome,
                offers=len(offers),
                fetch_seconds=cur_stats.get("fetch_seconds"),
                queue_wait=cur_stats.get("queue_wait"),
                search_id=search_id,
                attempt_tracker=attempt_tracker,
                stop_reason=cur_stats.get("stop_reason"),
                t_first_offer=cur_stats.get("t_first_offer"),
                t_last_improve=cur_stats.get("t_last_improve"),
                role=role,
            )
            try:
                if site.name in _FUSE_STATE and offers:
                    _FUSE_STATE[site.name]["degraded"] = False
            except Exception:
                pass
            if getattr(settings, "price_timeline", True) and offers:
                try:
                    t = asyncio.create_task(
                        _record_timeline_bg(settings, site, snap, q, search_id, used_dev)
                    )
                    _timeline_tasks.add(t)
                    t.add_done_callback(_timeline_tasks.discard)
                except Exception as exc:
                    log.warning("Не удалось запустить фоновую задачу price_timeline: %s", exc)
            return offers, page_url, partial

        if last_exc:
            raise last_exc
        raise RuntimeError("не удалось получить данные ни с одного телефона")
    else:
        snap = await fetch_snapshot(
            url,
            site,
            headless=settings.headless,
            profile_dir=settings.storage_dir / "profiles" / site.name,
            debug_dir=settings.debug_dir,
        )
        if snap.blocked:
            raise RuntimeError("сайт показал капчу или блокировку")
        partial = False
        offers, page_url, partial = await _process_snapshot(site, snap, q, settings, partial=partial)
        if getattr(settings, "price_timeline", True) and offers:
            try:
                t = asyncio.create_task(
                    _record_timeline_bg(settings, site, snap, q, search_id, None)
                )
                _timeline_tasks.add(t)
                t.add_done_callback(_timeline_tasks.discard)
            except Exception as exc:
                log.warning("Не удалось запустить фоновую задачу price_timeline: %s", exc)
        return offers, page_url, partial


async def _fallback_url(site: SiteConfig, q: Query, settings: Settings) -> str | None:
    """Ссылка на выдачу сайта для ручной проверки, когда парсер не сработал."""
    try:
        if site.source == "api":
            return search_url(q, settings.tp_marker)
        extra = await resolve_extra(site, q)
        return build_url(site, q, extra=extra)
    except Exception:
        return None


def _is_signature_mismatch(exc: TypeError) -> bool:
    """TypeError от вызова с лишним именованным аргументом (старая сигнатура), а не ошибка внутри вызова."""
    return "unexpected keyword argument" in str(exc)


async def _run_site(
    site: SiteConfig,
    q: Query,
    settings: Settings,
    *,
    priority: int = 0,
    weight: int = 0,
    guard: SearchGuard | None = None,
    device_order: list[str] | None = None,
    search_id: str | None = None,
    now: float | None = None,
    search_started: float | None = None,
    deep: bool = False,
    hedge_tracker: Any | None = None,
) -> SiteResult:
    if q.return_date and not site.url_template_rt:
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error="для этого сайта обратные билеты пока не поддерживаются",
        )
    if priority == 0:
        # запрос к SQLite — в потоке: предохранитель зовётся на старте каждого сайта каждого поиска
        is_fused, had_captcha = await check_site_fuse_async(site.name, settings, now=now, consume_probe=True)
        if is_fused:
            return SiteResult(
                site=site.name,
                ok=False,
                offers=[],
                error="сайт временно недоступен",
                captcha=had_captcha,
                elapsed=0.0,
                priority=priority,
            )
    t0 = time.monotonic()
    g = guard if guard is not None else globals()["guard"]
    stats: dict[str, Any] = {}
    attempt_tracker: list[int] = [0]
    if (priority > 0 or deep) and getattr(site, "phone_full_wait_seconds", None) is not None:
        actual_wait_sec = site.phone_full_wait_seconds
    else:
        actual_wait_sec = getattr(site, "phone_wait_seconds", 0) or 0
    weight = int(actual_wait_sec or 0)

    async def _fetch_browser(exclude: set[str] | None = None) -> tuple[list[Offer], str | None, bool]:
        enable_hedge = (
            priority == 0
            and not deep
            and getattr(settings, "hedge", True)
            and phone_enabled(settings, site)
            and exclude is None
        )
        if enable_hedge:
            try:
                from .hedge import hedge_risky, get_hedge_stats, HedgeTracker
                from .droidhub import try_acquire_free, release

                ht = hedge_tracker
                if ht is None:
                    ht = HedgeTracker(max_hedges=int(getattr(settings, "hedge_max_per_search", 2)))

                cands = candidates(settings, site, priority=0)
                primary_dev = (device_order[0] if device_order else (cands[0] if cands else None))

                # попытки пары за 7 дней читаются из SQLite — в потоке, чтобы старт сайта не держал цикл
                if (
                    primary_dev
                    and len(cands) >= 2
                    and await run_metrics_io(hedge_risky, site.name, primary_dev, settings, now=now)
                ):
                    stats_info = await run_metrics_io(get_hedge_stats, site.name, primary_dev, settings, now=now)
                    if stats_info.get("is_fail_risky"):
                        delay = float(getattr(settings, "hedge_delay_seconds", 0.0))
                    else:
                        p50 = float(stats_info.get("p50", 0.0))
                        delay = p50 if p50 > 0 else float(getattr(settings, "hedge_delay_seconds", 0.0))
                    delay = max(0.0, delay)

                    async def _run_copy(
                        target_d: str | None,
                        role: str,
                        already_acq: bool,
                    ) -> tuple[list[Offer], str | None, bool, dict[str, Any]]:
                        c_stats: dict[str, Any] = {}
                        tr = [0]
                        d_order = [target_d] if target_d else device_order
                        try:
                            o, u, p = await _from_browser(
                                site,
                                q,
                                settings,
                                priority=priority,
                                weight=weight,
                                stats=c_stats,
                                device_order=d_order,
                                search_id=search_id,
                                attempt_tracker=tr,
                                search_started=search_started,
                                now=now,
                                deep=deep,
                                target_device=target_d,
                                already_acquired=already_acq,
                                role=role,
                            )
                            return o, u, p, c_stats
                        finally:
                            if already_acq and target_d:
                                try:
                                    await release(target_d)
                                except Exception:
                                    pass

                    primary_task = asyncio.create_task(
                        _run_copy(primary_dev, role="primary", already_acq=False)
                    )

                    hedge_started = False
                    hedge_task = None
                    hedge_dev = None

                    if delay > 0:
                        try:
                            done, _ = await asyncio.wait([primary_task], timeout=delay)
                            if primary_task in done:
                                exc = primary_task.exception()
                                if exc is None:
                                    o, u, p, c_st = primary_task.result()
                                    if o:
                                        stats.update(c_st)
                                        return o, u, p
                        except Exception:
                            pass

                    # Если основная копия уже завершилась успешно с офферами — возвращаем её
                    if primary_task.done() and primary_task.exception() is None:
                        o, u, p, c_st = primary_task.result()
                        if o:
                            stats.update(c_st)
                            return o, u, p

                    # Пытаемся запустить хедж
                    if await ht.try_acquire(site.name):
                        hedge_dev = try_acquire_free(cands, exclude=primary_dev)
                        if hedge_dev is None:
                            await ht.release(site.name)
                        else:
                            hedge_started = True
                            hedge_task = asyncio.create_task(
                                _run_copy(hedge_dev, role="hedge", already_acq=True)
                            )

                    if not hedge_started:
                        o, u, p, c_st = await primary_task
                        stats.update(c_st)
                        return o, u, p

                    tasks = {primary_task, hedge_task}
                    first_done, remaining = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                    winner_data = None
                    winner_task = None
                    for t in first_done:
                        if t.exception() is None:
                            o, u, p, c_st = t.result()
                            if o:
                                winner_data = (o, u, p, c_st)
                                winner_task = t
                                break

                    if winner_data is not None:
                        loser = (tasks - {winner_task}).pop()
                        loser.cancel()
                        try:
                            await loser
                        except (asyncio.CancelledError, Exception):
                            pass
                        o, u, p, c_st = winner_data
                        stats.update(c_st)
                        return o, u, p

                    # Если первая копия закончилась ошибкой/пусто — ждём вторую
                    if remaining:
                        other_task = remaining.pop()
                        try:
                            o, u, p, c_st = await other_task
                            stats.update(c_st)
                            return o, u, p
                        except Exception as other_exc:
                            if primary_task.exception() is not None:
                                raise primary_task.exception()
                            raise other_exc
            except Exception as e:
                log.warning("Сбой в логике хеджей для %s: %s, откат на обычный запуск", site.name, e)
        try:
            if exclude:
                return await _from_browser(
                    site,
                    q,
                    settings,
                    priority=priority,
                    weight=weight,
                    stats=stats,
                    exclude_devices=exclude,
                    device_order=device_order,
                    search_id=search_id,
                    attempt_tracker=attempt_tracker,
                    search_started=search_started,
                    now=now,
                    deep=deep,
                )
            return await _from_browser(
                site,
                q,
                settings,
                priority=priority,
                weight=weight,
                stats=stats,
                device_order=device_order,
                search_id=search_id,
                attempt_tracker=attempt_tracker,
                search_started=search_started,
                now=now,
                deep=deep,
            )
        except TypeError as e:
            # повтор только для старой сигнатуры _from_browser; TypeError из парсера — настоящая ошибка,
            # повтор собрал бы страницу заново
            if not _is_signature_mismatch(e):
                raise
            try:
                if exclude:
                    return await _from_browser(
                        site, q, settings, priority=priority, weight=weight, stats=stats, exclude_devices=exclude
                    )
                return await _from_browser(
                    site,
                    q,
                    settings,
                    priority=priority,
                    weight=weight,
                    stats=stats,
                )
            except TypeError as e2:
                if not _is_signature_mismatch(e2):
                    raise
                return await _from_browser(site, q, settings)

    async def _fetch() -> tuple[list[Offer], str | None, bool]:
        if site.source == "api":
            offers, page_url = await _from_api(site, q, settings)
            return offers, page_url, False
        else:
            try:
                return await _fetch_browser()
            except ParserEmptyError as exc:
                if phone_enabled(settings, site):
                    cand_list = [
                        c
                        for c in (
                            device_order
                            or candidates(settings, site, priority=priority)
                        )
                        if c != stats.get("device")
                    ]
                    if not _can_retry_on_phone(
                        priority,
                        stats,
                        actual_wait_sec,
                        cand_list,
                        settings,
                        site.name,
                        search_started=search_started,
                        now=now,
                        deep=deep,
                    ):
                        raise
                    # снимок удаляем, только когда повтор точно пойдёт на другой телефон; иначе он остаётся владельцу
                    if cand_list:
                        _drop_debug_snapshot(exc)
                elif _hit_time_cap(priority, stats, actual_wait_sec):
                    # упёрлись в лимит ожидания: повтор потратит столько же времени и даст то же
                    raise
                else:
                    # без телефонов повтор идёт тем же браузером и при неудаче оставит свой снимок — как раньше
                    _drop_debug_snapshot(exc)
                bad_dev = stats.get("device")
                log.warning(
                    "Сайт %s вернул 0 вариантов (%s), повторная попытка через %s с (без телефона %s)",
                    site.name,
                    exc,
                    EMPTY_RETRY_DELAY,
                    bad_dev,
                )
                await asyncio.sleep(EMPTY_RETRY_DELAY)
                return await _fetch_browser({bad_dev} if bad_dev else None)

    try:
        (offers, page_url, partial), from_cache = await g.run(
            key_site=site.name,
            # глубокий поиск не делит кэш и «полёт» с быстрым: у него другая полнота
            key_query=(("deep", q) if deep else q),
            factory=_fetch,
            live=priority == 0,
            # пустые и неполные ответы не кэшируем: иначе следующий поиск получит «ничего не нашлось из кэша»
            cache_if=lambda r: bool(r[0]) and not r[2],
        )
        return SiteResult(
            site=site.name,
            ok=True,
            offers=list(offers),
            error=None,
            page_url=page_url,
            elapsed=time.monotonic() - t0,
            partial=partial,
            cached=from_cache,
            device=stats.get("device"),
            queue_wait=stats.get("queue_wait"),
            fetch_seconds=stats.get("fetch_seconds"),
            priority=priority,
        )
    except PreemptedError:
        raise
    except DroidhubError as e:
        log.warning("Ошибка droidhub на сайте %s: %s", site.name, e)
        is_captcha = "капч" in str(e).lower() or "блокировк" in str(e).lower()
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error=str(e),
            page_url=await _fallback_url(site, q, settings),
            elapsed=time.monotonic() - t0,
            partial=False,
            cached=False,
            captcha=is_captcha,
            device=stats.get("device"),
            queue_wait=stats.get("queue_wait"),
            fetch_seconds=stats.get("fetch_seconds"),
            priority=priority,
        )
    except ParserEmptyError as e:
        log.warning("Парсер сайта %s вернул 0 вариантов: %s", site.name, e)
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error=e.message,
            page_url=e.page_url or await _fallback_url(site, q, settings),
            elapsed=time.monotonic() - t0,
            partial=False,
            cached=False,
            captcha=False,
            device=stats.get("device"),
            queue_wait=stats.get("queue_wait"),
            fetch_seconds=stats.get("fetch_seconds"),
            priority=priority,
        )
    except Exception as e:
        log.exception("Ошибка при поиске на сайте %s: %s", site.name, e)
        if "разбор не уложился в таймаут" in str(e):
            err_msg = "разбор не уложился в таймаут"
        elif "TP_TOKEN" in str(e):
            err_msg = str(e)
        else:
            err_msg = friendly_error(e)
        is_captcha = "капч" in str(e).lower() or "блокировк" in str(e).lower() or "капч" in err_msg.lower()
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error=err_msg,
            page_url=await _fallback_url(site, q, settings),
            elapsed=time.monotonic() - t0,
            partial=False,
            cached=False,
            captcha=is_captcha,
            device=stats.get("device"),
            queue_wait=stats.get("queue_wait"),
            fetch_seconds=stats.get("fetch_seconds"),
            priority=priority,
        )


async def search(
    q: Query,
    settings: Settings,
    *,
    priority: int = 0,
    guard: SearchGuard | None = None,
    on_site_done: Callable[[SiteResult], Awaitable[None] | None] | None = None,
    search_id: str | None = None,
    now: float | None = None,
    deep: bool = False,
    fresh_cache: FreshCache | None = None,
    fresh_max_age: float = 0,
) -> list[SiteResult]:
    if search_id is None:
        search_id = uuid.uuid4().hex[:8]
    search_started = now if now is not None else time.monotonic()
    trial_stage_enabled = True
    try:
        trial_stage_enabled = bool(getattr(settings, "trial_stage", True))
    except Exception as e:
        log.warning("Ошибка чтения trial_stage: %s", e)

    try:
        if priority == 0 and trial_stage_enabled:
            sites = [
                s for s in settings.sites
                if s.enabled and getattr(s, "stage", "live") != "trial"
            ]
        else:
            sites = [s for s in settings.sites if s.enabled]
    except Exception as e:
        log.warning("Ошибка фильтрации trial-сайтов: %s", e)
        sites = [s for s in settings.sites if s.enabled]
    # живой поиск идёт всеми сайтами сразу (телефоны сами делят работу через очередь хаба);
    # подписки и фон остаются осторожными: не больше 2 сайтов разом
    limit = settings.search_parallel_live if priority == 0 else settings.search_parallel_bg
    sem = asyncio.Semaphore(max(1, int(limit)))
    g = guard if guard is not None else globals()["guard"]

    device_assignments: dict[str, list[str]] = {}
    if priority == 0 and getattr(settings, "live_assignment", True):
        try:
            from .dispatcher import plan_live_assignment

            # раскладка читает предохранитель и таблицу скоростей из SQLite — в потоке
            device_assignments = await run_metrics_io(plan_live_assignment, settings, sites, now=now)
        except Exception as e:
            log.warning("Ошибка при расчёте live assignment: %s", e)
            device_assignments = {}

    hedge_tracker = None
    if priority == 0 and getattr(settings, "hedge", True):
        try:
            from .hedge import HedgeTracker

            hedge_tracker = HedgeTracker(max_hedges=int(getattr(settings, "hedge_max_per_search", 2)))
        except Exception as e:
            log.warning("Ошибка инициализации HedgeTracker: %s", e)

    async def guarded(s: SiteConfig) -> SiteResult:
        cached_item = None
        if priority == 0 and not deep and fresh_cache is not None and fresh_max_age > 0:
            try:
                cached_item = fresh_cache.get(q, s.name, fresh_max_age)
            except Exception as e:
                log.warning("Ошибка чтения fresh_cache для сайта %s: %s", s.name, e)
                cached_item = None

        if cached_item is not None:
            res, _ = cached_item
        elif q.return_date and not s.url_template_rt:
            res = SiteResult(
                site=s.name,
                ok=False,
                offers=[],
                error="для этого сайта обратные билеты пока не поддерживаются",
            )
        elif priority == 0 and (await check_site_fuse_async(s.name, settings, now=now, consume_probe=False))[0]:
            is_fused, had_captcha = await check_site_fuse_async(s.name, settings, now=now, consume_probe=True)
            res = SiteResult(
                site=s.name,
                ok=False,
                offers=[],
                error="сайт временно недоступен",
                captcha=had_captcha,
            )
        else:
            dev_order = device_assignments.get(s.name)
            async with sem:
                try:
                    res = await _run_site(
                        s,
                        q,
                        settings,
                        priority=priority,
                        guard=g,
                        device_order=dev_order,
                        search_id=search_id,
                        now=now,
                        search_started=search_started,
                        deep=deep,
                        hedge_tracker=hedge_tracker,
                    )
                except PreemptedError:
                    if priority >= 1:
                        log.info("Сайт %s вытеснен живым поиском (пропуск без ошибки)", s.name)
                        return None
                    raise

            if fresh_cache is not None and res is not None:
                try:
                    fresh_cache.put(q, s.name, res)
                except Exception as e:
                    log.warning("Ошибка записи fresh_cache для сайта %s: %s", s.name, e)

        if on_site_done is not None and res is not None:
            try:
                ret = on_site_done(res)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:
                log.exception("Исключение в on_site_done для сайта %s: %s", s.name, e)
        return res

    results = await asyncio.gather(*(guarded(s) for s in sites))
    return [r for r in results if r is not None]
