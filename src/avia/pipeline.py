from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import time

from .collector import fetch_snapshot, Snapshot
from .config import Settings, SiteConfig
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
) -> tuple[list[Offer], str | None, bool]:
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
                raise

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
                if remaining_cands:
                    log.warning("Сайт %s: капча на телефоне %s, пробуем следующий телефон", site.name, used_dev)
                    continue
                raise RuntimeError("сайт показал капчу на телефоне")

            partial = not finished
            try:
                offers, page_url, partial = await _process_snapshot(site, snap, q, settings, partial=partial)
            except ParserEmptyError as exc:
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
                if remaining_cands:
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

            # Проверка: неполная выдача без офферов
            if partial and not offers:
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
                if remaining_cands:
                    log.warning("Сайт %s: неполная выдача без офферов на телефоне %s, пробуем следующий телефон", site.name, used_dev)
                    continue
                return offers, page_url, partial

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
    guard: SearchGuard | None = None,
) -> SiteResult:
    if q.return_date and not site.url_template_rt:
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error="для этого сайта обратные билеты пока не поддерживаются",
        )
    t0 = time.monotonic()
    g = guard if guard is not None else globals()["guard"]
    stats: dict[str, Any] = {}
    if priority > 0 and getattr(site, "phone_full_wait_seconds", None) is not None:
        actual_wait_sec = site.phone_full_wait_seconds
    else:
        actual_wait_sec = getattr(site, "phone_wait_seconds", 0) or 0
    weight = int(actual_wait_sec or 0)

    async def _fetch_browser() -> tuple[list[Offer], str | None, bool]:
        try:
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
                log.warning(
                    "Сайт %s вернул 0 вариантов (%s), повторная попытка через %s с",
                    site.name,
                    exc,
                    EMPTY_RETRY_DELAY,
                )
                await asyncio.sleep(EMPTY_RETRY_DELAY)
                return await _fetch_browser()

    try:
        (offers, page_url, partial), from_cache = await g.run(
            key_site=site.name,
            key_query=q,
            factory=_fetch,
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
        err_msg = str(e) if "TP_TOKEN" in str(e) else friendly_error(e)
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
) -> list[SiteResult]:
    sites = [s for s in settings.sites if s.enabled]
    # браузеры тяжёлые: не больше 2 одновременно (api-источники это не касается, но их мало)
    sem = asyncio.Semaphore(2)
    g = guard if guard is not None else globals()["guard"]

    async def guarded(s: SiteConfig) -> SiteResult:
        if q.return_date and not s.url_template_rt:
            return SiteResult(
                site=s.name,
                ok=False,
                offers=[],
                error="для этого сайта обратные билеты пока не поддерживаются",
            )
        async with sem:
            return await _run_site(s, q, settings, priority=priority, guard=g)

    return await asyncio.gather(*(guarded(s) for s in sites))
