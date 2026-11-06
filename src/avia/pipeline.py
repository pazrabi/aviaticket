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
from .dispatcher import check_site_fuse, _get_metrics, _FUSE_STATE
from .droidhub import (
    DroidhubError,
    PreferenceList,
    candidates,
    fetch_page_snapshot_any,
    phone_enabled,
)
from .extract import extract_offers
from .guard import SearchGuard, friendly_error
from .models import Offer, Query, SiteResult
from .parsers import PARSERS
from .parsers.travelpayouts import fetch_travelpayouts, search_url
from .sites import build_url
from .tutu_ids import resolve_city
from .yandex_ids import city_id

log = logging.getLogger("avia.pipeline")

# Общий SearchGuard на процесс (модульный объект для тестов с подменой)
guard = SearchGuard()

# Пауза перед повторной попыткой при 0 вариантов (ParserEmptyError)
EMPTY_RETRY_DELAY: float = 3.0


def _record_attempt_helper(
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
) -> None:
    role = "primary"
    if attempt_tracker is not None:
        if attempt_tracker[0] > 0:
            role = "retry"
        attempt_tracker[0] += 1
    metrics = _get_metrics(settings)
    if metrics is not None:
        source = "user" if priority == 0 else ("sub" if priority == 1 else "bg")
        metrics.record_attempt(
            site=site_name,
            device=device,
            source=source,
            role=role,
            outcome=outcome,
            offers=offers,
            fetch_seconds=fetch_seconds,
            queue_wait=queue_wait,
            search_id=search_id,
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
        return target
    except Exception as e:
        log.warning("Не удалось сохранить отладочный снимок для %s: %s", site.name, e)
        return None


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
            "origin_id": city_id(q.origin),
            "destination_id": city_id(q.destination),
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
        o.url_is_direct = bool(o.url and o.url != snap.url)
        o.url = o.url or snap.url
    return sorted(offers, key=lambda o: o.price), snap.url, partial


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
) -> tuple[list[Offer], str | None, bool]:
    if attempt_tracker is None:
        attempt_tracker = [0]
    extra = await resolve_extra(site, q)
    url = build_url(site, q, extra=extra)

    if priority > 0 and getattr(site, "phone_full_wait_seconds", None) is not None:
        wait_seconds = site.phone_full_wait_seconds
    else:
        wait_seconds = getattr(site, "phone_wait_seconds", 40)

    if priority > 0 and getattr(site, "phone_full_wait_for", None) is not None:
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

            try:
                snap, finished = await fetch_page_snapshot_any(
                    settings,
                    pref_arg,
                    url,
                    wait_seconds=wait_seconds,
                    capture_json=site.phone_capture_json,
                    wait_for=wait_for,
                    priority=priority,
                    weight=effective_weight,
                    stats=cur_stats,
                    patience=cur_patience,
                    site_name=site.name,
                )
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
                _record_attempt_helper(
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
                _record_attempt_helper(
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
            try:
                offers, page_url, partial = await _process_snapshot(site, snap, q, settings, partial=partial)
            except ParserEmptyError as exc:
                exc_str = str(exc).lower()
                outcome = "wrong_dates" if ("другие даты" in exc_str or getattr(exc, "tag", None) == "wrongdates") else "empty"
                _record_attempt_helper(
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
                )
                if remaining_cands and not _hit_time_cap(priority, cur_stats, wait_seconds):
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
                _record_attempt_helper(
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
                )
                raise
            except Exception as exc:
                exc_str = str(exc).lower()
                outcome = "captcha" if ("капч" in exc_str or "блокировк" in exc_str) else "error"
                _record_attempt_helper(
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
                )
                raise

            # Проверка: неполная выдача без офферов
            if partial and not offers:
                _record_attempt_helper(
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
                )
                if remaining_cands and not _hit_time_cap(priority, cur_stats, wait_seconds):
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
            _record_attempt_helper(
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
            )
            try:
                if site.name in _FUSE_STATE and offers:
                    _FUSE_STATE[site.name]["degraded"] = False
            except Exception:
                pass
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
        return await _process_snapshot(site, snap, q, settings, partial=partial)


async def _fallback_url(site: SiteConfig, q: Query, settings: Settings) -> str | None:
    """Ссылка на выдачу сайта для ручной проверки, когда парсер не сработал."""
    try:
        if site.source == "api":
            return search_url(q, settings.tp_marker)
        extra = await resolve_extra(site, q)
        return build_url(site, q, extra=extra)
    except Exception:
        return None


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
) -> SiteResult:
    if q.return_date and not site.url_template_rt:
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error="для этого сайта обратные билеты пока не поддерживаются",
        )
    if priority == 0:
        is_fused, had_captcha = check_site_fuse(site.name, settings, now=now, consume_probe=True)
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
    if priority > 0 and getattr(site, "phone_full_wait_seconds", None) is not None:
        actual_wait_sec = site.phone_full_wait_seconds
    else:
        actual_wait_sec = getattr(site, "phone_wait_seconds", 0) or 0
    weight = int(actual_wait_sec or 0)

    async def _fetch_browser(exclude: set[str] | None = None) -> tuple[list[Offer], str | None, bool]:
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
            )
        except TypeError:
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
            except TypeError:
                return await _from_browser(site, q, settings)

    async def _fetch() -> tuple[list[Offer], str | None, bool]:
        if site.source == "api":
            offers, page_url = await _from_api(site, q, settings)
            return offers, page_url, False
        else:
            try:
                return await _fetch_browser()
            except ParserEmptyError as exc:
                if exc.snapshot_path and exc.snapshot_path.exists():
                    try:
                        exc.snapshot_path.unlink()
                    except OSError:
                        pass
                if _hit_time_cap(priority, stats, actual_wait_sec):
                    # упёрлись в лимит ожидания: повтор потратит столько же времени и даст то же
                    raise
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
            key_query=q,
            factory=_fetch,
            live=priority == 0,
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
) -> list[SiteResult]:
    if search_id is None:
        search_id = uuid.uuid4().hex[:8]
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

            device_assignments = plan_live_assignment(settings, sites, now=now)
        except Exception as e:
            log.warning("Ошибка при расчёте live assignment: %s", e)
            device_assignments = {}

    async def guarded(s: SiteConfig) -> SiteResult:
        if q.return_date and not s.url_template_rt:
            res = SiteResult(
                site=s.name,
                ok=False,
                offers=[],
                error="для этого сайта обратные билеты пока не поддерживаются",
            )
        elif priority == 0 and check_site_fuse(s.name, settings, now=now, consume_probe=False)[0]:
            is_fused, had_captcha = check_site_fuse(s.name, settings, now=now, consume_probe=True)
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
                res = await _run_site(
                    s,
                    q,
                    settings,
                    priority=priority,
                    guard=g,
                    device_order=dev_order,
                    search_id=search_id,
                    now=now,
                )

        if on_site_done is not None:
            try:
                ret = on_site_done(res)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:
                log.exception("Исключение в on_site_done для сайта %s: %s", s.name, e)
        return res

    return await asyncio.gather(*(guarded(s) for s in sites))
