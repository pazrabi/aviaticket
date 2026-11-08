"""КТ4, работа 4: браузерные парсеры на заведомо плохих снимках.

Снимки — из `tests/fixtures/<сайт>/`, испорченные в памяти. Путь от снимка до `site_runs` идёт через
`pipeline.search` с подменённым сборщиком (`fetch_page_snapshot_any`) и `MetricsStore.record_results`,
как `bot._record_metrics`; база — временная `metrics.sqlite`. Без сети и без телефонов.

Порог плана: на каждом плохом снимке — понятная ошибка, и `parser_watch` считает сбор неудачным,
а не «0 вариантов» молча.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.formatting import _site_issue
from avia.guard import SearchGuard
from avia.metrics import MetricsStore
from avia.models import Leg, Offer, Query, SiteResult
from avia.parser_watch import STATUS_BROKEN, STATUS_DEGRADED, get_parser_statuses
from avia.parsers import PARSERS
from avia import pipeline
from avia.pipeline import (
    ParserEmptyError,
    _process_snapshot,
    _quick_parse_offers,
    _save_empty_snapshot,
    search,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEVICE = "dev1"
EMPTY_PAGE_MIN_CHARS = 200  # порог pipeline._process_snapshot для ParserEmptyError
# Длинная страница без билетов: шапка и подвал выдачи, ни цен, ни времени
LONG_PAGE_TEXT = "Поиск авиабилетов. Найдите дешёвые билеты на самолёт, сравните цены авиакомпаний. " * 6
SHORT_PAGE_TEXT = "502 Bad Gateway\nnginx"
CAPTCHA_PAGE_TEXT = "Подтвердите, что вы не робот"
GARBAGE = "мусор"


def _load(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Порча снимков по сайтам. Каждая функция портит свежую копию фикстуры (json.loads в _load).
# ---------------------------------------------------------------------------


def _aviasales_elements(data: Any) -> list[dict[str, Any]]:
    return data[0]["tickets"]


def _aviasales_drop_price(data: Any, ticket: dict[str, Any]) -> None:
    for proposal in ticket["proposals"]:
        proposal.pop("price")


def _aviasales_rename(data: Any) -> None:
    for ticket in _aviasales_elements(data):
        ticket["offers_v2"] = ticket.pop("proposals")


def _aviasales_garbage_time(data: Any, ticket: dict[str, Any]) -> None:
    legs = data[0]["flight_legs"]
    first = ticket["segments"][0]["flights"][0]
    leg = copy.deepcopy(legs[first])
    leg["local_departure_date_time"] = GARBAGE
    leg["departure_unix_timestamp"] = GARBAGE
    legs.append(leg)
    ticket["segments"][0]["flights"][0] = len(legs) - 1


def _kupibilet_elements(data: Any) -> list[dict[str, Any]]:
    return data["variants"]


def _kupibilet_drop_price(data: Any, variant: dict[str, Any]) -> None:
    variant.pop("price")


def _kupibilet_rename(data: Any) -> None:
    for variant in _kupibilet_elements(data):
        variant["cost"] = variant.pop("price")


def _kupibilet_garbage_time(data: Any, variant: dict[str, Any]) -> None:
    first = variant["segments"][0]["flights"][0]
    garbage_id = f"{first}_garbage"
    flight = copy.deepcopy(data["flights"][first])
    flight["departure_datetime"] = GARBAGE
    data["flights"][garbage_id] = flight
    variant["segments"][0]["flights"][0] = garbage_id


def _ozon_elements(data: Any) -> list[dict[str, Any]]:
    return data["state"]["flights"]


def _ozon_drop_price(data: Any, card: dict[str, Any]) -> None:
    card.pop("price")


def _ozon_rename(data: Any) -> None:
    for card in _ozon_elements(data):
        card["priceBlock"] = card.pop("price")


def _ozon_garbage_time(data: Any, card: dict[str, Any]) -> None:
    card["wayThereDetails"]["tripTime"]["text"] = GARBAGE


def _ttravel_elements(data: Any) -> list[dict[str, Any]]:
    return data["payload"]["offers"]


def _ttravel_drop_price(data: Any, offer: dict[str, Any]) -> None:
    offer.pop("price")


def _ttravel_rename(data: Any) -> None:
    for offer in _ttravel_elements(data):
        offer["cost"] = offer.pop("price")


def _ttravel_garbage_time(data: Any, offer: dict[str, Any]) -> None:
    flights = data["payload"]["flights"]
    flight = copy.deepcopy(flights[offer["flights"][0]])
    flight["flightSegments"][0]["departure"]["time"] = GARBAGE
    flights.append(flight)
    offer["flights"][0] = len(flights) - 1


def _tutu_elements(data: Any) -> list[dict[str, Any]]:
    return list(data[0]["offers"]["actual"].values())


def _tutu_drop_price(data: Any, offer: dict[str, Any]) -> None:
    for variant in offer["offerVariants"]:
        variant.pop("price")


def _tutu_rename(data: Any) -> None:
    for offer in _tutu_elements(data):
        offer["variantsV2"] = offer.pop("offerVariants")


def _tutu_garbage_time(data: Any, offer: dict[str, Any]) -> None:
    common = data[0]["dictionary"]["common"]
    route_id = offer["routeIds"][0]
    route = copy.deepcopy(common["routes"][route_id])
    first_seg = route["segmentIds"][0]
    segment = copy.deepcopy(common["segments"][first_seg])
    segment["departureDateTime"] = GARBAGE
    garbage_seg = f"{first_seg}garbage"
    garbage_route = f"{route_id}garbage"
    common["segments"][garbage_seg] = {**segment, "id": garbage_seg}
    common["routes"][garbage_route] = {**route, "id": garbage_route, "segmentIds": [garbage_seg, *route["segmentIds"][1:]]}
    offer["routeIds"] = [garbage_route, *offer["routeIds"][1:]]


def _yandex_elements(data: Any) -> list[dict[str, Any]]:
    return data["variants"]["fares"]


def _yandex_drop_price(data: Any, fare: dict[str, Any]) -> None:
    for price in fare["prices"]:
        price.pop("tariff")


def _yandex_rename(data: Any) -> None:
    for fare in _yandex_elements(data):
        fare["pricesV2"] = fare.pop("prices")


def _yandex_garbage_time(data: Any, fare: dict[str, Any]) -> None:
    flights = data["reference"]["flights"]
    first_key = fare["route"][0][0]
    flight = copy.deepcopy(next(f for f in flights if f["key"] == first_key))
    garbage_key = f"{first_key}garbage"
    flight["key"] = garbage_key
    flight["departure"]["local"] = GARBAGE
    flights.append(flight)
    fare["route"][0][0] = garbage_key


@dataclass(frozen=True)
class SiteCase:
    name: str
    payload_url: str
    fixture: str
    page_url: str
    query: Query
    normal_count: int  # билетов на целой фикстуре (как в тестах владельца)
    elements: Callable[[Any], list[dict[str, Any]]]
    drop_price: Callable[[Any, dict[str, Any]], None]
    rename_format: Callable[[Any], None]
    garbage_time: Callable[[Any, dict[str, Any]], None]

    def data(self) -> Any:
        return _load(self.fixture)

    def snapshot(self, data: Any, text: str = LONG_PAGE_TEXT, blocked: bool = False) -> Snapshot:
        return Snapshot(url=self.page_url, text=text, json_payloads=[(self.payload_url, json.dumps(data))],
                        blocked=blocked)

    def parse(self, snap: Snapshot) -> list[Offer]:
        return PARSERS[self.name](snap, self.query)


MOW_ALA = Query("MOW", "ALA", date(2026, 12, 10))
SITE_CASES = [
    SiteCase("aviasales", "https://www.aviasales.ru/search/v3.2/results?search_id=test",
             "aviasales/results_mow_ala.json", "https://www.aviasales.ru/search/MOW1012ALA1", MOW_ALA, 4,
             _aviasales_elements, _aviasales_drop_price, _aviasales_rename, _aviasales_garbage_time),
    SiteCase("kupibilet", "https://api-rs-lb.kupibilet.ru/frontend_search",
             "kupibilet/search_quick_mow_ala.json", "https://www.kupibilet.ru/search", Query("MOW", "ALA", date(2026, 12, 12)), 8,
             _kupibilet_elements, _kupibilet_drop_price, _kupibilet_rename, _kupibilet_garbage_time),
    SiteCase("ozon_travel", (FIXTURES_DIR / "ozon_travel" / "widget_url.txt").read_text(encoding="utf-8").strip(),
             "ozon_travel/search_oneway_kuf_evn_0112_widget.json", "https://www.ozon.ru/travel/flight/search",
             Query("KUF", "EVN", date(2026, 12, 1)), 30,
             _ozon_elements, _ozon_drop_price, _ozon_rename, _ozon_garbage_time),
    SiteCase("ttravel", "https://www.tbank-online.com/api/travel/flight/search/startStreaming?sessionId=test",
             "ttravel/search_ow_kuf_mow_1210.json",
             "https://www.tbank-online.com/travel/flights/one-way/KUF-MOW/12-10/?adults=1&children=0&infants=0&cabin=Y&composite=0",
             Query("KUF", "MOW", date(2026, 12, 10)), 60,
             _ttravel_elements, _ttravel_drop_price, _ttravel_rename, _ttravel_garbage_time),
    SiteCase("tutu", "https://offers-api.tutu.ru/avia/offers/v2", "tutu/offers_first_mow_ala.json",
             "https://avia.tutu.ru/offers/", Query("MOW", "ALA", date(2026, 12, 12)), 4,
             _tutu_elements, _tutu_drop_price, _tutu_rename, _tutu_garbage_time),
    SiteCase("yandex_travel", "https://travel.yandex.ru/api/avia/search/results?qid=test_qid&cont=0",
             "yandex_travel/results_cont0_mow_ala.json", "https://travel.yandex.ru/avia/search/result/",
             Query("MOW", "ALA", date(2026, 12, 12)), 6,
             _yandex_elements, _yandex_drop_price, _yandex_rename, _yandex_garbage_time),
]
CASE_IDS = [c.name for c in SITE_CASES]


def _offer_key(offer: Offer) -> tuple[Any, ...]:
    return (offer.price, offer.depart_at, offer.arrive_at, tuple(offer.flight_numbers or ()), offer.airline)


def _settings(tmp_path: Path, case: SiteCase, store: MetricsStore | None = None, phone: str = DEVICE,
              **extra: Any) -> Settings:
    site = SiteConfig(name=case.name, url_template="https://site.test/{origin}{destination}", source="browser",
                      parser=case.name, phone=phone)
    return Settings(sites=[site], storage_dir=tmp_path, metrics=store, droidhub_url="https://hub.test",
                    droidhub_token="hub-test-token", debug_dir=tmp_path / "debug", **extra)


@pytest.fixture(autouse=True)
def _no_empty_retry_delay(monkeypatch):
    monkeypatch.setattr("avia.pipeline.EMPTY_RETRY_DELAY", 0.0)


async def _search_with_snapshot(
    tmp_path: Path,
    monkeypatch,
    case: SiteCase,
    snap: Snapshot | list[Snapshot],
    *,
    priority: int = 0,
    phone: str = DEVICE,
    **extra: Any,
) -> tuple[SiteResult, MetricsStore]:
    """Поиск по одному сайту; сборщик отдаёт снимки по очереди сборов. Результат пишется в site_runs, как в боте."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    settings = _settings(tmp_path, case, store, phone=phone, **extra)
    queue = list(snap) if isinstance(snap, list) else None

    async def fake_fetch_any(_settings, cands, url, **kwargs):
        stats = kwargs.get("stats")
        if stats is not None:
            stats.update({"device": cands[0], "tried": [cands[0]], "queue_wait": 0.1, "fetch_seconds": 2.0})
        return (queue.pop(0) if queue is not None else snap), True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)
    results = await search(case.query, settings, priority=priority, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    store.record_results("user" if priority == 0 else "bg", results)
    assert len(results) == 1
    return results[0], store


def _debug_snapshots(tmp_path: Path) -> list[str]:
    debug_dir = tmp_path / "debug"
    return sorted(p.name for p in debug_dir.iterdir()) if debug_dir.exists() else []


def _site_runs(store: MetricsStore) -> list[dict[str, Any]]:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT site, ok, partial, captcha, error, offers, device FROM site_runs").fetchall()
    return [dict(r) for r in rows]


def _watch_status(tmp_path: Path, case: SiteCase, store: MetricsStore) -> str:
    """Статус parser_watch по одному сбору (порог числа сборов снят до 1)."""
    settings = _settings(tmp_path, case, store, parser_watch_min_runs=1)
    return get_parser_statuses(store, settings)[case.name]


# ---------------------------------------------------------------------------
# 0. Фикстуры целые: без порчи парсер не пропускает ни одного элемента
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
def test_intact_fixture_parses_all_offers(case):
    offers = case.parse(case.snapshot(case.data()))

    assert len(offers) == case.normal_count


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_intact_fixture_logs_no_skip_warning(tmp_path, case, caplog):
    """Целая выдача — без строки о пропущенных элементах."""
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(settings.sites[0], case.snapshot(case.data()), case.query, settings)

    assert len(offers) == case.normal_count
    assert not [r for r in caplog.records if "пропущено" in r.getMessage()]


# ---------------------------------------------------------------------------
# 1. Пустое содержимое — [] без исключения
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.parametrize("body", ["", "{}", "[]", "null"], ids=["empty", "obj", "list", "null"])
def test_empty_payload_returns_empty_list(case, body):
    snap = Snapshot(url=case.page_url, text="", json_payloads=[(case.payload_url, body)])

    assert case.parse(snap) == []


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
def test_no_payloads_returns_empty_list(case):
    assert case.parse(Snapshot(url=case.page_url, text="", json_payloads=[])) == []


# ---------------------------------------------------------------------------
# 2. Капча (snap.blocked = True, как ставит сборщик) — сбор не удачный, captcha = 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_captcha_snapshot_recorded_as_failed_captcha_run(tmp_path, monkeypatch, case):
    snap = case.snapshot(case.data(), text=CAPTCHA_PAGE_TEXT, blocked=True)

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, snap)

    assert (result.ok, result.captcha, result.offers) == (False, True, [])
    assert _site_runs(store) == [
        {"site": case.name, "ok": 0, "partial": 0, "captcha": 1, "error": result.error, "offers": 0, "device": DEVICE}
    ]
    assert store.watch_stats(0)[case.name] == {"runs": 1, "ok": 0, "partial": 0}
    assert _watch_status(tmp_path, case, store) == STATUS_BROKEN


# ---------------------------------------------------------------------------
# 3. Изменённый формат: страница длинная, ключевые поля переименованы — ParserEmptyError, ok = 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_renamed_format_raises_parser_empty_error(tmp_path, case):
    data = case.data()
    case.rename_format(data)
    settings = _settings(tmp_path, case)
    snap = case.snapshot(data)
    assert len(snap.text.strip()) > EMPTY_PAGE_MIN_CHARS

    with pytest.raises(ParserEmptyError, match="формат выдачи мог измениться"):
        await _process_snapshot(settings.sites[0], snap, case.query, settings)


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_renamed_format_recorded_as_failed_run(tmp_path, monkeypatch, case):
    data = case.data()
    case.rename_format(data)

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, case.snapshot(data))

    assert result.ok is False
    assert "формат выдачи мог измениться" in (result.error or "")
    assert _site_runs(store) == [
        {"site": case.name, "ok": 0, "partial": 0, "captcha": 0, "error": result.error, "offers": 0, "device": DEVICE}
    ]
    assert store.watch_stats(0)[case.name] == {"runs": 1, "ok": 0, "partial": 0}
    assert _watch_status(tmp_path, case, store) == STATUS_BROKEN
    assert _site_issue(result).startswith("⚠️")


# ---------------------------------------------------------------------------
# 3а. Отладочный снимок при ParserEmptyError: остаётся, если повтора на другом телефоне не будет
# ---------------------------------------------------------------------------


def _renamed_snapshot(case: SiteCase) -> Snapshot:
    data = case.data()
    case.rename_format(data)
    return case.snapshot(data)


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_renamed_format_keeps_debug_snapshot_when_no_retry(tmp_path, monkeypatch, case):
    """Последний кандидат: повтора нет — снимок остаётся владельцу."""
    result, _ = await _search_with_snapshot(tmp_path, monkeypatch, case, _renamed_snapshot(case))

    assert result.ok is False
    snaps = _debug_snapshots(tmp_path)
    assert len(snaps) == 1 and snaps[0].startswith(f"{case.name}_empty_")


def _retry_answers(monkeypatch, answers: list[bool]) -> list[bool]:
    """Подменяет решение о повторе: первый ответ — в _from_browser, второй — в _fetch."""
    calls: list[bool] = []

    def fake_can_retry(*args: Any, **kwargs: Any) -> bool:
        answer = answers[len(calls)]
        calls.append(answer)
        return answer

    monkeypatch.setattr("avia.pipeline._can_retry_on_phone", fake_can_retry)
    return calls


@pytest.mark.asyncio
async def test_fetch_retry_on_other_phone_removes_debug_snapshot(tmp_path, monkeypatch):
    case = SITE_CASES[0]
    calls = _retry_answers(monkeypatch, [False, True])

    result, _ = await _search_with_snapshot(
        tmp_path, monkeypatch, case, [_renamed_snapshot(case), case.snapshot(case.data())],
        phone="dev1,dev2", hedge=False,
    )

    assert calls == [False, True]
    assert (result.ok, len(result.offers), result.device) == (True, case.normal_count, "dev2")
    assert _debug_snapshots(tmp_path) == []


@pytest.mark.asyncio
async def test_fetch_no_retry_keeps_debug_snapshot(tmp_path, monkeypatch):
    case = SITE_CASES[0]
    calls = _retry_answers(monkeypatch, [False, False])

    result, _ = await _search_with_snapshot(
        tmp_path, monkeypatch, case, [_renamed_snapshot(case)], phone="dev1,dev2", hedge=False,
    )

    assert calls == [False, False]
    assert result.ok is False
    assert len(_debug_snapshots(tmp_path)) == 1


@pytest.mark.asyncio
async def test_fetch_retry_on_same_phone_keeps_debug_snapshot(tmp_path, monkeypatch):
    """Фон, один телефон: повтор пойдёт на тот же телефон, а не на другой — снимок первой попытки остаётся."""
    case = SITE_CASES[0]

    result, _ = await _search_with_snapshot(
        tmp_path, monkeypatch, case, [_renamed_snapshot(case), case.snapshot(case.data())], priority=2,
    )

    assert (result.ok, len(result.offers)) == (True, case.normal_count)
    assert len(_debug_snapshots(tmp_path)) == 1


@pytest.mark.asyncio
async def test_in_loop_retry_on_other_phone_removes_debug_snapshot(tmp_path, monkeypatch):
    """Как было: повтор на втором телефоне внутри _from_browser удаляет снимок первого."""
    case = SITE_CASES[0]

    result, _ = await _search_with_snapshot(
        tmp_path, monkeypatch, case, [_renamed_snapshot(case), case.snapshot(case.data())],
        phone="dev1,dev2", hedge=False,
    )

    assert (result.ok, result.device) == (True, "dev2")
    assert _debug_snapshots(tmp_path) == []


def _make_debug_files(debug_dir: Path, names: list[str], start_mtime: float = 1_700_000_000.0) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        path = debug_dir / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (start_mtime + i, start_mtime + i))


SNAPSHOT_MAX = 100
OLD_SNAPSHOTS = [f"aviasales_empty_20261101_{i:06d}.json" for i in range(SNAPSHOT_MAX + 5)]
NOT_SNAPSHOTS = [
    "aviasales.png",  # скриншот сборщика
    "aviasales.txt",  # текст страницы сборщика
    "notes_empty_20261101_000001.txt",
    "aviasales_empty_latest.json",
    "aviasales_other_20261101_000001.json",
]


def test_debug_snapshots_capped_oldest_removed(tmp_path):
    """Сверх DEBUG_SNAPSHOTS_MAX удаляются самые старые снимки — только по шаблону имени и только в debug_dir."""
    case = SITE_CASES[0]
    settings = _settings(tmp_path, case, debug_snapshots_max=SNAPSHOT_MAX)
    debug_dir = tmp_path / "debug"
    _make_debug_files(debug_dir, OLD_SNAPSHOTS)
    _make_debug_files(debug_dir, NOT_SNAPSHOTS)
    _make_debug_files(debug_dir / "recon", OLD_SNAPSHOTS[:3])

    new_path = _save_empty_snapshot(settings.sites[0], _renamed_snapshot(case), settings)

    snapshots = sorted(p.name for p in debug_dir.iterdir() if p.is_file() and p.name.endswith(".json")
                       and p.name not in NOT_SNAPSHOTS)
    assert new_path is not None and new_path.exists()
    assert len(snapshots) == SNAPSHOT_MAX
    assert set(OLD_SNAPSHOTS[:6]).isdisjoint(snapshots)
    assert set(OLD_SNAPSHOTS[6:]) <= set(snapshots)
    assert all((debug_dir / name).exists() for name in NOT_SNAPSHOTS)
    assert sorted(p.name for p in (debug_dir / "recon").iterdir()) == OLD_SNAPSHOTS[:3]


def test_debug_snapshots_not_capped_when_limit_off(tmp_path):
    """DEBUG_SNAPSHOTS_MAX = 0 — предел выключен, как было до правки."""
    case = SITE_CASES[0]
    settings = _settings(tmp_path, case, debug_snapshots_max=0)
    _make_debug_files(tmp_path / "debug", OLD_SNAPSHOTS)

    _save_empty_snapshot(settings.sites[0], _renamed_snapshot(case), settings)

    assert len(list((tmp_path / "debug").iterdir())) == len(OLD_SNAPSHOTS) + 1


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_all_elements_crooked_logs_one_skip_line_then_parser_empty_error(tmp_path, case, caplog):
    """Все элементы кривые на длинной странице — строка о пропуске, дальше ParserEmptyError, как сейчас."""
    data = case.data()
    case.rename_format(data)
    total = len(case.elements(data))
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING), pytest.raises(ParserEmptyError):
        await _process_snapshot(settings.sites[0], case.snapshot(data), case.query, settings)

    lines = [r.getMessage() for r in caplog.records if "пропущено" in r.getMessage()]
    assert lines == [f"Сайт {case.name}: пропущено {total} из {total} элементов выдачи"]


# ---------------------------------------------------------------------------
# 4. Один кривой элемент среди нормальных — остальные на месте, кривой пропущен, одна строка WARNING
# ---------------------------------------------------------------------------


def _intact_offers(case: SiteCase) -> list[Offer]:
    return case.parse(case.snapshot(case.data()))


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
def test_one_element_without_price_is_skipped_rest_kept(case):
    intact = {_offer_key(o) for o in _intact_offers(case)}
    data = case.data()
    case.drop_price(data, case.elements(data)[0])

    offers = case.parse(case.snapshot(data))

    assert len(offers) == case.normal_count - 1
    assert {_offer_key(o) for o in offers} < intact


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
def test_one_element_with_garbage_time_does_not_break_the_rest(case):
    intact = {_offer_key(o) for o in _intact_offers(case)}
    data = case.data()
    case.garbage_time(data, case.elements(data)[0])

    offers = case.parse(case.snapshot(data))
    kept = {_offer_key(o) for o in offers}

    assert len(offers) >= case.normal_count - 1
    assert len(kept & intact) >= case.normal_count - 1


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_one_element_with_garbage_time_skipped_as_crooked(tmp_path, case, caplog):
    """Мусор вместо времени — кривой элемент: пропущен, в строке «пропущено», а не «неверные даты»."""
    intact = {_offer_key(o) for o in _intact_offers(case)}
    data = case.data()
    total = len(case.elements(data))
    case.garbage_time(data, case.elements(data)[0])
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(settings.sites[0], case.snapshot(data), case.query, settings)

    messages = [r.getMessage() for r in caplog.records]
    assert len(offers) == case.normal_count - 1
    assert {_offer_key(o) for o in offers} < intact
    assert [m for m in messages if "пропущено" in m] == [f"Сайт {case.name}: пропущено 1 из {total} элементов выдачи"]
    assert not [m for m in messages if "неверными датами" in m]


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
def test_quick_parse_drops_garbage_time(case):
    """Быстрый разбор (FAST_STOP) не видит цену билета, который полный разбор выбросит."""
    intact = {_offer_key(o) for o in _intact_offers(case)}
    data = case.data()
    case.garbage_time(data, case.elements(data)[0])
    site = SiteConfig(name=case.name, url_template="https://site.test/{origin}{destination}", parser=case.name)

    offers = _quick_parse_offers(site, case.snapshot(data), case.query)

    assert len(offers) == case.normal_count - 1
    assert {_offer_key(o) for o in offers} < intact


TIME_QUERY = Query("MOW", "LED", date(2026, 12, 10))
TIME_OFFERS = [
    # (цена, время вылета, время прилёта, вылет ISO, обратное плечо, останется ли)
    (1000, "07:05", "9:10", "2026-12-10T07:05", None, True),
    (1100, None, None, None, None, True),  # время неизвестно — билет остаётся, как раньше
    (1200, "", "", "", None, True),  # пустое время — тоже
    (1300, "07:05", "09:10", "2026-12-10T07:05:00+03:00", None, True),
    (1400, "мусор", "09:10", None, None, False),
    (1500, "25:00", "09:10", None, None, False),
    (1600, "07:05", "09:10", "мусор", None, False),
    (1700, "07:05", "09:10", None, Leg(depart_time="мусор"), False),
    (1800, "07:05", "09:10", None, Leg(depart_time="18:40", arrive_time="20:15"), True),
]


@pytest.mark.asyncio
async def test_time_check_drops_only_present_malformed_times(tmp_path, monkeypatch, caplog):
    """Одна проверка в _process_snapshot: время есть, но не ЧЧ:ММ (или ISO не ГГГГ-ММ-ДДTЧЧ:ММ) — кривой элемент."""
    def fake_parser(snap, q):
        return [Offer(site="fake_times", price=price, depart_time=dep, arrive_time=arr, depart_at=dep_at, back=back)
                for price, dep, arr, dep_at, back, _ in TIME_OFFERS]

    monkeypatch.setitem(PARSERS, "fake_times", fake_parser)
    site = SiteConfig(name="fake_times", url_template="https://site.test/{origin}{destination}", parser="fake_times")
    settings = Settings(sites=[site], storage_dir=tmp_path, debug_dir=tmp_path / "debug")

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(site, Snapshot(url="u", text=""), TIME_QUERY, settings)

    assert sorted(o.price for o in offers) == [p for p, *_, keep in TIME_OFFERS if keep]
    assert [r.getMessage() for r in caplog.records if "пропущено" in r.getMessage()] == [
        f"Сайт fake_times: пропущено 4 из {len(TIME_OFFERS)} элементов выдачи"
    ]


@pytest.mark.parametrize("text", [None, ""], ids=["no_trip_time", "empty_trip_time"])
def test_ozon_card_without_time_kept(text):
    """Ozon: времени в карточке нет — билет остаётся без времени, как раньше; мусор — пропуск (тест выше)."""
    case = next(c for c in SITE_CASES if c.name == "ozon_travel")
    data = case.data()
    details = case.elements(data)[0]["wayThereDetails"]
    if text is None:
        details.pop("tripTime")
    else:
        details["tripTime"]["text"] = text

    offers = case.parse(case.snapshot(data))

    assert len(offers) == case.normal_count
    assert offers.skipped == 0
    assert sum(1 for o in offers if o.depart_time is None) == 1


ALL_FIXTURES = [
    ("aviasales", name) for name in ("results_final_mow_ala.json", "results_mow_ala.json",
                                     "results_partial_mow_ala.json", "results_rt_kuf_evn.json")
] + [("kupibilet", name) for name in ("search_full_mow_ala.json", "search_quick_mow_ala.json", "search_rt_kuf_evn.json")] + [
    ("ozon_travel", name) for name in ("search_oneway_kuf_evn_0112_widget.json", "search_rt_kuf_evn_0112_0812_widget.json")
] + [("ttravel", name) for name in ("search_ow_kuf_mow_1210.json", "search_rt_kuf_svo_1210_1217.json")] + [
    ("tutu", name) for name in ("offers_first_mow_ala.json", "offers_last_mow_ala.json", "offers_rt_a_kuf_evn.json",
                                "offers_rt_cheapest_kuf_evn.json", "offers_rt_final_kuf_evn.json")
] + [("yandex_travel", name) for name in ("results_cont0_mow_ala.json", "results_cont_last_mow_ala.json",
                                          "results_rt_a_kuf_evn.json", "results_rt_b_kuf_evn.json",
                                          "results_rt_final_kuf_evn.json")]
TEXT_PAGES = [
    ("ozon_travel", "text_only_oneway_kuf_evn_0112_page.txt", Query("KUF", "EVN", date(2026, 12, 1))),
    ("ozon_travel", "text_only_oneway_kuf_evn_0412_page.txt", Query("KUF", "EVN", date(2026, 12, 4))),
    ("ozon_travel", "text_rt_kuf_evn_0112_0812_page.txt", Query("KUF", "EVN", date(2026, 12, 1), return_date=date(2026, 12, 8))),
    ("yandex_travel", "text_only_oneway_mow_led_1012_page.txt", Query("MOW", "LED", date(2026, 12, 10))),
]
OW_QUERY = Query("MOW", "ALA", date(2026, 12, 10))
RT_QUERY = Query("KUF", "EVN", date(2026, 12, 1), return_date=date(2026, 12, 8))


def _fixture_snapshot(site: str, name: str) -> Snapshot:
    case = next(c for c in SITE_CASES if c.name == site)
    body = (FIXTURES_DIR / site / name).read_text(encoding="utf-8")
    if name.endswith(".txt"):
        return Snapshot(url=case.page_url, text=body, json_payloads=[])
    return Snapshot(url=case.page_url, text="", json_payloads=[(case.payload_url, body)])


def _json_fixture_query(name: str) -> Query:
    return RT_QUERY if "_rt" in name else OW_QUERY


@pytest.mark.parametrize(
    ("site", "name", "query"),
    [(s, n, _json_fixture_query(n)) for s, n in ALL_FIXTURES] + TEXT_PAGES,
    ids=[n for _, n in ALL_FIXTURES] + [n for _, n, _ in TEXT_PAGES],
)
def test_intact_fixtures_have_no_skips_and_no_bad_times(site, name, query):
    """Все 21 целая JSON-фикстура и 4 текстовые страницы: ни одного пропуска, ни одного кривого времени."""
    offers = PARSERS[site](_fixture_snapshot(site, name), query)

    assert offers
    assert offers.skipped == 0
    assert not [o for o in offers if pipeline._has_bad_time(o)]


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_one_crooked_element_logs_one_warning_with_site_name(tmp_path, case, caplog):
    data = case.data()
    total = len(case.elements(data))
    case.drop_price(data, case.elements(data)[0])
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(settings.sites[0], case.snapshot(data), case.query, settings)

    assert len(offers) == case.normal_count - 1
    lines = [r for r in caplog.records if "пропущено" in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in lines] == [
        (logging.WARNING, f"Сайт {case.name}: пропущено 1 из {total} элементов выдачи")
    ]


def _duplicate_first_element(case: SiteCase, data: Any) -> None:
    """Тот же элемент выдачи приходит дважды (у tutu предложения — словарь по id)."""
    if case.name == "tutu":
        actual = data[0]["offers"]["actual"]
        first_key = next(iter(actual))
        actual[f"{first_key}dup"] = copy.deepcopy(actual[first_key])
        return
    elements = case.elements(data)
    elements.append(copy.deepcopy(elements[0]))


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_duplicate_element_not_counted_as_skipped(tmp_path, case, caplog):
    """Дубль элемента — не кривой: строки о пропуске нет, билеты те же."""
    intact = {_offer_key(o) for o in _intact_offers(case)}
    data = case.data()
    _duplicate_first_element(case, data)
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(settings.sites[0], case.snapshot(data), case.query, settings)

    assert {_offer_key(o) for o in offers} == intact
    assert not [r for r in caplog.records if "пропущено" in r.getMessage()]


@pytest.mark.asyncio
async def test_yandex_route_missing_from_reference_counted_as_skipped(tmp_path, caplog):
    """Второй этап yandex: тариф с ценой, но рейса маршрута нет в справочнике — билет не собран, пропуск учтён."""
    case = next(c for c in SITE_CASES if c.name == "yandex_travel")
    data = case.data()
    case.elements(data)[0]["route"][0][0] = "missing-flight-key"
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(settings.sites[0], case.snapshot(data), case.query, settings)

    assert len(offers) == case.normal_count - 1
    assert [r.getMessage() for r in caplog.records if "пропущено" in r.getMessage()] == [
        f"Сайт yandex_travel: пропущено 1 из {case.normal_count} элементов выдачи"
    ]


@pytest.mark.asyncio
async def test_aviasales_counts_each_delivery_of_a_crooked_ticket(tmp_path, caplog):
    """Aviasales шлёт выдачу порциями, билеты в них повторяются: N и K — по доставкам, не по уникальным билетам."""
    case = SITE_CASES[0]
    data = case.data()
    case.drop_price(data, case.elements(data)[0])
    body = json.dumps(data)
    snap = Snapshot(url=case.page_url, text=LONG_PAGE_TEXT, json_payloads=[(case.payload_url, body)] * 2)
    settings = _settings(tmp_path, case)

    with caplog.at_level(logging.WARNING):
        offers, _, _ = await _process_snapshot(settings.sites[0], snap, case.query, settings)

    assert len(offers) == case.normal_count - 1
    assert [r.getMessage() for r in caplog.records if "пропущено" in r.getMessage()] == [
        f"Сайт aviasales: пропущено 2 из {2 * case.normal_count} элементов выдачи"
    ]


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
def test_quick_parse_does_not_log_skip_line(case, caplog):
    """Быстрый разбор (FAST_STOP) зовёт парсер много раз за сбор — строка о пропуске только на полный разбор."""
    data = case.data()
    case.drop_price(data, case.elements(data)[0])
    site = SiteConfig(name=case.name, url_template="https://site.test/{origin}{destination}", parser=case.name)

    with caplog.at_level(logging.WARNING):
        offers = _quick_parse_offers(site, case.snapshot(data), case.query)

    assert len(offers) == case.normal_count - 1
    assert not [r for r in caplog.records if "пропущено" in r.getMessage()]


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_one_crooked_element_run_stays_successful(tmp_path, monkeypatch, case):
    data = case.data()
    case.drop_price(data, case.elements(data)[0])

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, case.snapshot(data))

    assert result.ok is True
    assert len(result.offers) == case.normal_count - 1
    assert _site_runs(store)[0]["ok"] == 1


# ---------------------------------------------------------------------------
# 5. Короткая страница без капчи («502 Bad Gateway») — что попадает в site_runs и что видит человек
# ---------------------------------------------------------------------------


def _short_page(case: SiteCase, payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    return Snapshot(url=case.page_url, text=SHORT_PAGE_TEXT, json_payloads=payloads or [])


async def _assert_short_page_as_before(result: SiteResult, store: MetricsStore, tmp_path: Path, case: SiteCase) -> None:
    """Как до флага: «0 вариантов», сбор удачный (ok = 1, partial = 1), человек видит «билетов не нашлось»."""
    assert (result.ok, result.offers, result.partial, result.error) == (True, [], True, None)
    assert _site_runs(store) == [
        {"site": case.name, "ok": 1, "partial": 1, "captcha": 0, "error": None, "offers": 0, "device": DEVICE}
    ]
    assert store.watch_stats(0)[case.name] == {"runs": 1, "ok": 1, "partial": 1}
    assert _watch_status(tmp_path, case, store) == STATUS_DEGRADED
    assert "билетов не нашлось" in _site_issue(result)
    assert "неполная выдача" in _site_issue(result)
    assert _debug_snapshots(tmp_path) == []


@pytest.mark.parametrize("flag", [None, False], ids=["flag_default", "flag_0"])
@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_short_page_flag_off_behaves_as_before(tmp_path, monkeypatch, case, flag):
    """SHORT_PAGE_FAIL = 0 (по умолчанию): короткая страница — как до правки."""
    extra = {} if flag is None else {"short_page_fail": flag}

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, _short_page(case), **extra)

    await _assert_short_page_as_before(result, store, tmp_path, case)


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_short_page_flag_on_counted_as_failed_run(tmp_path, monkeypatch, case):
    """SHORT_PAGE_FAIL = 1: короткая страница без JSON-ответов — неудачный сбор и строка ошибки сайта."""
    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, _short_page(case), short_page_fail=True)

    assert (result.ok, result.offers, result.captcha) == (False, [], False)
    assert result.error == "сайт не отдал выдачу: «502 Bad Gateway nginx»"
    assert _site_runs(store) == [
        {"site": case.name, "ok": 0, "partial": 0, "captcha": 0, "error": result.error, "offers": 0, "device": DEVICE}
    ]
    assert store.watch_stats(0)[case.name] == {"runs": 1, "ok": 0, "partial": 0}
    assert _watch_status(tmp_path, case, store) == STATUS_BROKEN
    issue = _site_issue(result)
    assert issue.startswith("⚠️") and "сайт не отдал выдачу" in issue
    assert "билетов не нашлось" not in issue
    snaps = _debug_snapshots(tmp_path)
    assert len(snaps) == 1 and snaps[0].startswith(f"{case.name}_short_")


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_short_page_flag_on_with_json_answer_behaves_as_before(tmp_path, monkeypatch, case):
    """Сайт ответил JSON, но билетов нет (тело «{}») — это не «сайт не отдал выдачу»: как до правки."""
    snap = _short_page(case, [(case.payload_url, "{}")])

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, snap, short_page_fail=True)

    await _assert_short_page_as_before(result, store, tmp_path, case)


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_short_page_flag_on_with_html_in_captured_answer_fails(tmp_path, monkeypatch, case):
    """Перехваченный ответ выдачи — HTML 502, а не JSON: JSON-ответов нет, сбор неудачный."""
    snap = _short_page(case, [(case.payload_url, "<html><body>502 Bad Gateway</body></html>")])

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, snap, short_page_fail=True)

    assert result.ok is False
    assert _site_runs(store)[0]["ok"] == 0


@pytest.mark.asyncio
async def test_short_page_flag_on_captcha_still_captcha(tmp_path, monkeypatch):
    """Капча остаётся капчей и при SHORT_PAGE_FAIL = 1."""
    case = SITE_CASES[0]
    snap = Snapshot(url=case.page_url, text=CAPTCHA_PAGE_TEXT, json_payloads=[], blocked=True)

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, snap, short_page_fail=True)

    assert (result.ok, result.captcha) == (False, True)
    assert _site_runs(store)[0]["captcha"] == 1


# ---------------------------------------------------------------------------
# 6. Флаги в config: SHORT_PAGE_FAIL (0/1, по умолчанию 0), DEBUG_SNAPSHOTS_MAX (по умолчанию 100)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [(None, False), ("0", False), ("1", True), ("true", True), ("off", False)],
)
def test_load_settings_short_page_fail(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("SHORT_PAGE_FAIL", raising=False)
    else:
        monkeypatch.setenv("SHORT_PAGE_FAIL", env)

    assert load_settings().short_page_fail is expected


@pytest.mark.parametrize(("env", "expected"), [(None, 100), ("5", 5), ("0", 0), ("abc", 100)])
def test_load_settings_debug_snapshots_max(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("DEBUG_SNAPSHOTS_MAX", raising=False)
    else:
        monkeypatch.setenv("DEBUG_SNAPSHOTS_MAX", env)

    assert load_settings().debug_snapshots_max == expected
