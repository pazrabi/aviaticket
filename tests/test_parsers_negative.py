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
from pathlib import Path
import sqlite3
from typing import Any, Callable

import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.formatting import _site_issue
from avia.guard import SearchGuard
from avia.metrics import MetricsStore
from avia.models import Offer, Query, SiteResult
from avia.parser_watch import STATUS_BROKEN, STATUS_DEGRADED, get_parser_statuses
from avia.parsers import PARSERS
from avia.pipeline import ParserEmptyError, _process_snapshot, _quick_parse_offers, search

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


def _settings(tmp_path: Path, case: SiteCase, store: MetricsStore | None = None, **extra: Any) -> Settings:
    site = SiteConfig(name=case.name, url_template="https://site.test/{origin}{destination}", source="browser",
                      parser=case.name, phone=DEVICE)
    return Settings(sites=[site], storage_dir=tmp_path, metrics=store, droidhub_url="https://hub.test",
                    droidhub_token="hub-test-token", debug_dir=tmp_path / "debug", **extra)


@pytest.fixture(autouse=True)
def _no_empty_retry_delay(monkeypatch):
    monkeypatch.setattr("avia.pipeline.EMPTY_RETRY_DELAY", 0.0)


async def _search_with_snapshot(tmp_path: Path, monkeypatch, case: SiteCase, snap: Snapshot) -> tuple[SiteResult, MetricsStore]:
    """Живой поиск по одному сайту с одним телефоном; результат пишется в site_runs, как в боте."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    settings = _settings(tmp_path, case, store)

    async def fake_fetch_any(_settings, cands, url, **kwargs):
        stats = kwargs.get("stats")
        if stats is not None:
            stats.update({"device": cands[0], "tried": [cands[0]], "queue_wait": 0.1, "fetch_seconds": 2.0})
        return snap, True

    monkeypatch.setattr("avia.pipeline.fetch_page_snapshot_any", fake_fetch_any)
    results = await search(case.query, settings, priority=0, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    store.record_results("user", results)
    assert len(results) == 1
    return results[0], store


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Отладочный снимок не доживает до владельца: _process_snapshot сохраняет его в debug_dir, "
        "а _fetch удаляет его сразу при ParserEmptyError (pipeline.py:1208–1212), ещё до решения о повторе — "
        "и когда повтора не будет. Предложение в docs/audit/kt4_parsers.md"
    ),
)
@pytest.mark.asyncio
async def test_renamed_format_keeps_debug_snapshot_when_no_retry(tmp_path, monkeypatch):
    case = SITE_CASES[0]
    data = case.data()
    case.rename_format(data)

    result, _ = await _search_with_snapshot(tmp_path, monkeypatch, case, case.snapshot(data))

    assert result.ok is False
    assert list((tmp_path / "debug").glob(f"{case.name}_empty_*.json"))


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


_GARBAGE_TIME_KEPT = pytest.mark.xfail(
    strict=True,
    reason=(
        "Кривой элемент не пропущен: билет с мусором вместо времени вылета остаётся в выдаче человека "
        "(kupibilet, ttravel, tutu — время «мусор», ozon_travel — без времени). У aviasales и yandex_travel "
        "мусор уходит и в depart_at, и билет отбрасывает filter_wrong_dates как «неверные даты». Правка меняет "
        "поведение — решает приёмщик, предложение в docs/audit/kt4_parsers.md"
    ),
)


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name, marks=() if c.name in ("aviasales", "yandex_travel") else _GARBAGE_TIME_KEPT)
     for c in SITE_CASES],
)
@pytest.mark.asyncio
async def test_one_element_with_garbage_time_not_shown(tmp_path, case):
    """То, что получит человек после _process_snapshot: билета с мусором вместо времени нет, остальные на месте."""
    intact = {_offer_key(o) for o in _intact_offers(case)}
    data = case.data()
    case.garbage_time(data, case.elements(data)[0])
    settings = _settings(tmp_path, case)

    offers, _, _ = await _process_snapshot(settings.sites[0], case.snapshot(data), case.query, settings)

    assert len(offers) == case.normal_count - 1
    assert {_offer_key(o) for o in offers} < intact


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


@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_short_page_without_captcha_current_behaviour(tmp_path, monkeypatch, case):
    """Сейчас: «0 вариантов», сбор удачный (ok = 1, partial = 1), человек видит «билетов не нашлось»."""
    snap = Snapshot(url=case.page_url, text=SHORT_PAGE_TEXT, json_payloads=[])

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, snap)

    assert (result.ok, result.offers, result.partial, result.error) == (True, [], True, None)
    assert _site_runs(store) == [
        {"site": case.name, "ok": 1, "partial": 1, "captcha": 0, "error": None, "offers": 0, "device": DEVICE}
    ]
    assert store.watch_stats(0)[case.name] == {"runs": 1, "ok": 1, "partial": 1}
    assert _watch_status(tmp_path, case, store) == STATUS_DEGRADED
    assert "билетов не нашлось" in _site_issue(result)
    assert "неполная выдача" in _site_issue(result)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Порог плана не выполняется: короткая страница без капчи (меньше 200 символов, «502 Bad Gateway») "
        "даёт 0 вариантов и удачный сбор ok = 1, partial = 1; parser_watch считает его удачным, человек видит "
        "«билетов не нашлось». Предложение — в docs/audit/kt4_parsers.md, решает приёмщик"
    ),
)
@pytest.mark.parametrize("case", SITE_CASES, ids=CASE_IDS)
@pytest.mark.asyncio
async def test_short_page_without_captcha_counted_as_failed_run(tmp_path, monkeypatch, case):
    snap = Snapshot(url=case.page_url, text=SHORT_PAGE_TEXT, json_payloads=[])

    result, store = await _search_with_snapshot(tmp_path, monkeypatch, case, snap)

    assert result.ok is False
    assert store.watch_stats(0)[case.name]["ok"] == 0
