"""Управление живыми образцами ответов сайтов и проверка парсеров на них."""
from __future__ import annotations

from datetime import date, datetime
import functools
import gzip
from html import escape
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from .collector import Snapshot
from .models import Offer, Query
from .parsers import PARSERS

log = logging.getLogger("avia.samples")

ParserFn = Callable[[Snapshot, Query], list[Offer]]


def _redact_tokens(text: str) -> str:
    """Удаляет токены и ключи API из текста."""
    return re.sub(
        r'((?:[?&]|\b)(?:token|access_token|api_key|key|secret)=)[^&"\s]+',
        r'\1[REDACTED]',
        text,
        flags=re.I,
    )


def save_sample(
    site: str,
    snap: Snapshot,
    q: Query,
    samples_dir: Path | str,
    max_bytes: int = 3_000_000,
    keep_max: int = 3,
    min_interval_seconds: float = 86400.0,
    now: float | None = None,
) -> Path | None:
    """Сохраняет сырой снимок страницы в samples_dir/<site>/ (не чаще раза в сутки на сайт и тип поиска, максимум 3)."""
    now_ts = time.time() if now is None else float(now)
    search_type = "roundtrip" if q.return_date else "oneway"
    site_dir = Path(samples_dir) / site
    site_dir.mkdir(parents=True, exist_ok=True)

    # Проверка суточного лимита для данного типа поиска (one-way / round-trip)
    existing_type_files = sorted(site_dir.glob(f"sample_{search_type}_*.json.gz"))
    for f in existing_type_files:
        try:
            mtime = f.stat().st_mtime
            if now_ts - mtime < min_interval_seconds:
                return None
        except OSError:
            pass

    dep_str = q.depart.isoformat() if hasattr(q.depart, "isoformat") else str(q.depart)
    ret_str = q.return_date.isoformat() if q.return_date and hasattr(q.return_date, "isoformat") else (str(q.return_date) if q.return_date else None)

    data = {
        "site": site,
        "search_type": search_type,
        "ts": now_ts,
        "query": {
            "origin": q.origin,
            "destination": q.destination,
            "depart": dep_str,
            "return_date": ret_str,
            "adults": q.adults,
        },
        "snapshot": {
            "url": snap.url,
            "text": snap.text,
            "json_payloads": snap.json_payloads,
            "blocked": getattr(snap, "blocked", False),
        },
    }

    content = json.dumps(data, ensure_ascii=False)
    content = _redact_tokens(content)

    raw_bytes = content.encode("utf-8")
    if len(raw_bytes) > max_bytes:
        allowed_text_len = max(0, len(snap.text) - (len(raw_bytes) - max_bytes))
        data["snapshot"]["text"] = snap.text[:allowed_text_len]
        content = json.dumps(data, ensure_ascii=False)
        content = _redact_tokens(content)
        raw_bytes = content.encode("utf-8")
        if len(raw_bytes) > max_bytes:
            log.warning("Размер образца для %s превышает %d байт, пропускаем", site, max_bytes)
            return None

    gz_bytes = gzip.compress(raw_bytes)
    dt_str = datetime.fromtimestamp(now_ts).strftime("%Y%m%d_%H%M%S")
    target_path = site_dir / f"sample_{search_type}_{int(now_ts)}_{dt_str}.json.gz"
    target_path.write_bytes(gz_bytes)

    # Устанавливаем время модификации файла, если задано now
    try:
        os.utime(target_path, (now_ts, now_ts))
    except OSError:
        pass

    # Ротация: хранить не больше keep_max (по умолчанию 3) последних образцов на сайт
    all_files = sorted(
        [p for p in site_dir.glob("sample_*.json.gz") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    if len(all_files) > keep_max:
        for old_file in all_files[:-keep_max]:
            try:
                old_file.unlink()
            except OSError:
                pass

    return target_path


def _make_fixture_snapshot_and_query(site: str, path: Path, content: str) -> tuple[Snapshot, Query]:
    """Фолбэк для работы с сырыми тестовыми фикстурами из tests/fixtures."""
    # Определяем параметры запроса и URL по сайту
    q = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 10), adults=1)
    url = "https://example.com"
    payloads: list[tuple[str, str]] = []

    if site == "aviasales":
        url = "https://www.aviasales.ru/search/MOW1012ALA1"
        payloads = [("https://www.aviasales.ru/search/v3.2/results?search_id=test", content)]
    elif site == "kupibilet":
        url = "https://www.kupibilet.ru/search"
        payloads = [("https://api-rs-lb.kupibilet.ru/frontend_search", content)]
    elif site == "onetwotrip":
        url = "https://www.onetwotrip.com/ru/f/search/0212KUFEVN?sc=E&p=1_0_0"
        q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 2), adults=1)
        payloads = [("https://www.onetwotrip.com/_avia-search-proxy/search/v3", content)]
    elif site == "tutu":
        url = "https://avia.tutu.ru/offers/?class=Y&travelers=1&route[0]=491-12122026-117"
        q = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 12), adults=1)
        payloads = [("https://offers-api.tutu.ru/avia/offers/v2", content)]
    elif site == "yandex_travel":
        url = "https://travel.yandex.ru/avia/search/result/?adult_seats=1&fromId=c213&toId=c22177&when=2026-12-12"
        q = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 12), adults=1)
        payloads = [("https://travel.yandex.ru/api/avia/search/results?qid=test&cont=0", content)]
    elif site == "ozon_travel":
        url = "https://www.ozon.ru/travel/flight/search"
        q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1), adults=1)
        payloads = [("https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=%2Ftravel%2Fflight%2Fsearch", content)]
    else:
        payloads = [(url, content)]

    snap = Snapshot(url=url, text="Поиск билетов", json_payloads=payloads)
    return snap, q


def load_sample(path: Path | str) -> tuple[str, Snapshot, Query]:
    """Загружает образец из файла (.json.gz или .json) и возвращает (site, snap, query)."""
    p = Path(path)
    raw = p.read_bytes()
    if p.name.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        content = gzip.decompress(raw).decode("utf-8")
    else:
        content = raw.decode("utf-8")

    try:
        data = json.loads(content)
    except Exception:
        # Если это текстовая фикстура
        site = p.parent.name
        snap = Snapshot(url="https://example.com", text=content, json_payloads=[])
        q = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 1), adults=1)
        return site, snap, q

    if isinstance(data, dict) and "snapshot" in data:
        site = data.get("site", p.parent.name)
        q_d = data.get("query", {})
        dep_val = date.fromisoformat(q_d["depart"]) if isinstance(q_d.get("depart"), str) else (q_d.get("depart") or date(2026, 12, 1))
        ret_val = date.fromisoformat(q_d["return_date"]) if q_d.get("return_date") and isinstance(q_d["return_date"], str) else None
        q = Query(
            origin=q_d.get("origin", "MOW"),
            destination=q_d.get("destination", "ALA"),
            depart=dep_val,
            return_date=ret_val,
            adults=q_d.get("adults", 1),
        )
        snap_d = data["snapshot"]
        snap = Snapshot(
            url=snap_d.get("url", ""),
            text=snap_d.get("text", ""),
            json_payloads=[(str(item[0]), str(item[1])) for item in snap_d.get("json_payloads", [])],
            blocked=bool(snap_d.get("blocked", False)),
        )
        return site, snap, q

    # Фолбэк на случай сырой фикстуры
    site = p.parent.name
    snap, q = _make_fixture_snapshot_and_query(site, p, content)
    return site, snap, q


def check_samples(
    samples_dir: Path | str,
    parsers: dict[str, ParserFn] | None = None,
) -> dict[str, dict[str, Any]]:
    """Прогоняет парсеры на образцах из samples_dir.

    Возвращает:
    {site: {"offers_count": int, "min_price": int | None, "ok": bool, "error": str | None, "sample_path": Path}}
    """
    parsers_map = parsers if parsers is not None else PARSERS
    s_dir = Path(samples_dir)
    results: dict[str, dict[str, Any]] = {}

    if not s_dir.exists():
        return results

    # Перебираем поддиректории сайтов
    site_dirs = [d for d in s_dir.iterdir() if d.is_dir()]
    if not site_dirs:
        sample_files = list(s_dir.glob("sample_*.json.gz")) + list(s_dir.glob("*.json"))
        if sample_files:
            site_dirs = [s_dir]

    for site_d in sorted(site_dirs):
        site_name = site_d.name
        # Ищем образцы (самый свежий sample_*.json.gz или *.json)
        gz_files = sorted(site_d.glob("sample_*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not gz_files:
            gz_files = sorted(
                [f for f in site_d.glob("*.json*") if f.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        if not gz_files:
            continue

        latest_sample = gz_files[0]
        try:
            site_from_sample, snap, q = load_sample(latest_sample)
            target_site = site_from_sample or site_name
            parser = parsers_map.get(target_site)
            if not parser:
                results[target_site] = {
                    "offers_count": 0,
                    "min_price": None,
                    "ok": False,
                    "error": f"парсер для {target_site!r} не найден",
                    "sample_path": latest_sample,
                }
                continue

            offers = parser(snap, q)
            count = len(offers)
            min_p = min((o.price for o in offers), default=None) if count > 0 else None
            ok = count > 0
            results[target_site] = {
                "offers_count": count,
                "min_price": min_p,
                "ok": ok,
                "error": None if ok else "0 вариантов",
                "sample_path": latest_sample,
            }
        except Exception as e:
            results[site_name] = {
                "offers_count": 0,
                "min_price": None,
                "ok": False,
                "error": str(e),
                "sample_path": latest_sample,
            }

    return results


def run_parser_check_cli(samples_dir: Path | str | None = None) -> int:
    """CLI команда parser-check: проверяет парсеры на образцах и печатает результаты."""
    from .config import load_settings

    if samples_dir is None:
        samples_dir = load_settings().storage_dir / "samples"
    s_path = Path(samples_dir)

    results = check_samples(s_path)
    if not results:
        print(f"Образцы не найдены в {s_path}")
        return 0

    has_failure = False
    for site, res in sorted(results.items()):
        if res["ok"]:
            p_str = f", мин. цена {res['min_price']} ₽" if res['min_price'] is not None else ""
            print(f"{site}: {res['offers_count']} вар.{p_str}")
        else:
            has_failure = True
            print(f"{site}: 0 вариантов (провал)")

    return 1 if has_failure else 0


def install_sample_hook(samples_dir: Path | str) -> None:
    """Устанавливает перехватчик в PARSERS для автоматического сохранения образцов при успешных поисках."""
    for name, parser_fn in list(PARSERS.items()):
        if getattr(parser_fn, "_is_sample_hooked", False):
            continue

        def make_wrapper(site_name: str, fn: ParserFn):
            @functools.wraps(fn)
            def wrapper(snap: Snapshot, q: Query) -> list[Offer]:
                offers = fn(snap, q)
                if offers:
                    try:
                        save_sample(site_name, snap, q, samples_dir)
                    except Exception:
                        log.exception("ошибка сохранения образца %s", site_name)
                return offers

            wrapper._is_sample_hooked = True
            if hasattr(fn, "results_complete"):
                wrapper.results_complete = getattr(fn, "results_complete")
            return wrapper

        PARSERS[name] = make_wrapper(name, parser_fn)


class sample_capture_context:
    """Контекстный менеджер для перехвата и сохранения снимков парсеров во время поиска."""

    def __init__(self, samples_dir: Path | str | None):
        self.samples_dir = samples_dir
        self.orig_parsers: dict[str, Any] = {}

    def __enter__(self):
        if not self.samples_dir:
            return self
        for name, fn in list(PARSERS.items()):
            # Если уже обёрнут — сохраняем исходную функцию
            orig = getattr(fn, "_orig_parser_fn", fn)
            self.orig_parsers[name] = orig

            def make_wrapper(site_name: str, real_fn: ParserFn):
                @functools.wraps(real_fn)
                def wrapper(snap: Snapshot, q: Query) -> list[Offer]:
                    offers = real_fn(snap, q)
                    if offers:
                        try:
                            save_sample(site_name, snap, q, self.samples_dir)
                        except Exception:
                            log.exception("ошибка сохранения образца %s", site_name)
                    return offers

                wrapper._orig_parser_fn = real_fn
                if hasattr(real_fn, "results_complete"):
                    wrapper.results_complete = getattr(real_fn, "results_complete")
                return wrapper

            PARSERS[name] = make_wrapper(name, orig)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for name, fn in self.orig_parsers.items():
            PARSERS[name] = fn


def check_samples_with_cooldown(
    samples_dir: Path | str,
    metrics: Any = None,
    cooldown_hours: float = 6.0,
    now: float | None = None,
) -> list[str]:
    """Раз в неделю запускает проверку образцов и при сбое возвращает сообщения админам с кулдауном."""
    results = check_samples(samples_dir)
    failures: list[str] = []
    now_ts = time.time() if now is None else float(now)
    cooldown_sec = cooldown_hours * 3600.0

    for site, res in sorted(results.items()):
        state_key = f"sample_check_{site}"
        last_alert_ts = 0.0
        last_status = "ok"
        if metrics is not None:
            st = metrics.get_watch_state(state_key)
            if st:
                last_alert_ts = st.get("last_alert_ts", 0.0)
                last_status = st.get("last_status", "ok")

        if not res["ok"]:
            if now_ts - last_alert_ts >= cooldown_sec or last_status != "broken":
                msg = f"⚠️ Проверка парсеров на образцах: сайт {escape(site)} — 0 вариантов (провал)"
                failures.append(msg)
                if metrics is not None:
                    metrics.set_watch_state(
                        site=state_key,
                        last_status="broken",
                        last_alert_ts=now_ts,
                        last_alert_status="broken",
                    )
        else:
            if last_status == "broken":
                msg = f"✅ Проверка парсеров на образцах: сайт {escape(site)} восстановился ({res['offers_count']} вар.)"
                failures.append(msg)
                if metrics is not None:
                    metrics.set_watch_state(
                        site=state_key,
                        last_status="ok",
                        last_alert_ts=now_ts,
                        last_alert_status="ok",
                    )

    return failures
