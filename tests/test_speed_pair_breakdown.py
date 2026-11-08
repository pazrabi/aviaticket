"""КТ4, работа 3 (П9): через сколько запросов `speed.py` владельца выводит сломанную пару «сайт × телефон».

Порог плана: пара, которая сломалась, выводится из живого поиска не позже чем через 10 запросов.
«Выведена» — `order_candidates(site, [A, B], priority=0)` не ставит A первой, хотя B здоров.

Запрос — строка `site_runs` пары A живого поиска (priority 0), как её пишут `droidhub._record_failure`
и `MetricsStore.record_results`. После каждой записи `droidhub` сбрасывает кэш таблицы (`invalidate`),
поэтому здесь таблица тоже пересчитывается после каждого запроса.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from avia.config import Settings, SiteConfig
from avia.metrics import MetricsStore
from avia.speed import SpeedTable

SITE_NAME = "as"
BROKEN = "A"  # пара, которая ломается
HEALTHY = "B"  # второй телефон, все сборы удачные
DAY = 86400.0
BREAK_TS = 30 * DAY  # момент поломки
HISTORY_STEP = 600.0  # старые сборы — раз в 10 минут в последние 6 суток (внутри окна 7 дней)
REQUEST_STEP = 60.0  # запрос раз в минуту: три провала подряд укладываются в карантин 600 с
CHECK_DELAY = 1.0  # раскладку смотрим через секунду после записи запроса
LIMIT_REQUESTS = 10  # порог П9
MAX_REQUESTS = 60  # дальше не ищем: «не выведена»
HISTORY_OK = 50
SHORT_HISTORY = 3  # меньше speed_min_samples = 5
A_FETCH = 4.0  # A быстрее B, поэтому до поломки A первая
B_FETCH = 8.0
FAIL_FETCH = 1.0  # провал приходит быстро
OFFERS = 5
QUARANTINE_SECONDS = 600.0
CAPTCHA_FIRST_COOLDOWN = 3600.0
CAPTCHA_SECOND_COOLDOWN = 10800.0

OK = "ok"
FAIL = "fail"  # 502, таймаут, пустая выдача: ok = 0
CAPTCHA = "captcha"
EMPTY_PARTIAL = "empty_partial"  # ok = 1, partial = 1, 0 билетов


def _record(store: MetricsStore, device: str, kind: str, ts: float, priority: int = 0, fetch: float = A_FETCH) -> None:
    """Пишет один сбор пары в site_runs так, как его пишет бот."""
    source = "user" if priority == 0 else "bg"
    if kind == OK:
        store.record_run(source, SITE_NAME, ok=True, partial=False, captcha=False, error=None,
                         offers=OFFERS, elapsed=fetch, ts=ts, device=device, fetch_seconds=fetch, priority=priority)
    elif kind == FAIL:
        store.record_run(source, SITE_NAME, ok=False, partial=False, captcha=False, error="502",
                         offers=0, elapsed=FAIL_FETCH, ts=ts, device=device, fetch_seconds=FAIL_FETCH,
                         priority=priority)
    elif kind == CAPTCHA:
        store.record_run(source, SITE_NAME, ok=False, partial=False, captcha=True, error="капча",
                         offers=0, elapsed=FAIL_FETCH, ts=ts, device=device, fetch_seconds=FAIL_FETCH,
                         priority=priority)
    elif kind == EMPTY_PARTIAL:
        store.record_run(source, SITE_NAME, ok=True, partial=True, captcha=False, error="неполная выдача",
                         offers=0, elapsed=FAIL_FETCH, ts=ts, device=device, fetch_seconds=FAIL_FETCH,
                         priority=priority)
    else:
        raise ValueError(f"неизвестный исход сбора: {kind}")


@dataclass(frozen=True)
class Pair:
    store: MetricsStore
    table: SpeedTable
    site: SiteConfig


def _make_pair(tmp_path: Path, a_history: int = HISTORY_OK, b_history: int = HISTORY_OK) -> Pair:
    """История за 7 дней: у A и B только удачные сборы, A быстрее."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    start = BREAK_TS - 6 * DAY
    for i in range(a_history):
        _record(store, BROKEN, OK, ts=start + i * HISTORY_STEP, fetch=A_FETCH)
    for i in range(b_history):
        _record(store, HEALTHY, OK, ts=start + i * HISTORY_STEP + 1.0, fetch=B_FETCH)
    site = SiteConfig(name=SITE_NAME, url_template="https://test", phone=f"{BROKEN},{HEALTHY}")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    return Pair(store=store, table=SpeedTable(metrics_path=db_path, settings=settings), site=site)


def _live_order(pair: Pair, now: float) -> list[str]:
    pair.table.invalidate()
    return list(pair.table.order_candidates(pair.site, [BROKEN, HEALTHY], priority=0, now=now))


def _bg_order(pair: Pair, now: float) -> list[str]:
    pair.table.invalidate()
    return list(pair.table.order_candidates(pair.site, [BROKEN, HEALTHY], priority=2, now=now))


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    pattern: Callable[[int], str]  # номер запроса (с 1) → исход сбора
    a_history: int = HISTORY_OK
    b_history: int = HISTORY_OK


def _requests_until_withdrawn(tmp_path: Path, scenario: Scenario) -> int | None:
    """Номер запроса, после которого A больше не первая; 0 — не первая и до поломки; None — не выведена."""
    pair = _make_pair(tmp_path, a_history=scenario.a_history, b_history=scenario.b_history)
    if _live_order(pair, now=BREAK_TS)[0] != BROKEN:
        return 0
    for k in range(1, MAX_REQUESTS + 1):
        ts = BREAK_TS + k * REQUEST_STEP
        _record(pair.store, BROKEN, scenario.pattern(k), ts=ts)
        if _live_order(pair, now=ts + CHECK_DELAY)[0] != BROKEN:
            return k
    return None


ALL_FAIL = Scenario("all_fail", "ломается совсем", lambda k: FAIL)
CAPTCHA_FIRST = Scenario("captcha_first", "капча на первом же сборе", lambda k: CAPTCHA)
ALTERNATE = Scenario("alternate", "через раз (ok, провал, …)", lambda k: OK if k % 2 == 1 else FAIL)
TWO_OF_THREE = Scenario("two_of_three", "два провала из трёх (провал, провал, ok, …)",
                        lambda k: OK if k % 3 == 0 else FAIL)
EMPTY_PARTIALS = Scenario("empty_partial", "partial без билетов подряд", lambda k: EMPTY_PARTIAL)
SHORT_A_LONG_B = Scenario("short_a_long_b", "короткая история A, у B 50 сборов", lambda k: FAIL,
                          a_history=SHORT_HISTORY)
SHORT_BOTH = Scenario("short_both", "короткая история у A и B, A ломается совсем", lambda k: FAIL,
                      a_history=SHORT_HISTORY, b_history=SHORT_HISTORY)
ALTERNATE_SHORT_B = Scenario("alternate_short_b", "доп.: через раз, у B меньше 5 сборов на сайте",
                             ALTERNATE.pattern, b_history=SHORT_HISTORY)

# Факт на speed.py владельца (kt1-all 74854fa): запрос, после которого пара выведена.
FACTS: dict[str, int | None] = {
    ALL_FAIL.key: 3,
    CAPTCHA_FIRST.key: 1,
    ALTERNATE.key: 20,
    TWO_OF_THREE.key: 14,
    EMPTY_PARTIALS.key: 3,
    SHORT_A_LONG_B.key: 0,
    SHORT_BOTH.key: 3,
    ALTERNATE_SHORT_B.key: None,
}

_XFAIL_ALTERNATE = pytest.mark.xfail(
    strict=True,
    reason=(
        "П9 не выполняется: «через раз» выведена только после 20 запросов (порог 10). Провалов подряд "
        "не больше 1 — карантина нет; доля успехов считается по последним 30 сборам пары, и wilson_lower "
        "падает ниже 0,6 только на 20 успехах из 30 (0,5806)"
    ),
)
_XFAIL_TWO_OF_THREE = pytest.mark.xfail(
    strict=True,
    reason=(
        "П9 не выполняется: «два провала из трёх» выведена только после 14 запросов (порог 10). Провалов "
        "подряд не больше 2 — карантина нет; wilson_lower по последним 30 сборам ниже 0,6 только на 20 "
        "успехах из 30"
    ),
)
_XFAIL_ALTERNATE_SHORT_B = pytest.mark.xfail(
    strict=True,
    reason=(
        "П9 не выполняется: «через раз» при B с 3 сборами на сайте не выведена за 60 запросов. A уходит "
        "в запасные (wilson_lower < 0,6), B тоже запасной (n < speed_min_samples), запасные сортируются "
        "только по цене C, а быстрые провалы держат C у A (5,0 с) ниже, чем у B (8,0 с)"
    ),
)


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(ALL_FAIL, id=ALL_FAIL.key),
        pytest.param(CAPTCHA_FIRST, id=CAPTCHA_FIRST.key),
        pytest.param(ALTERNATE, id=ALTERNATE.key, marks=_XFAIL_ALTERNATE),
        pytest.param(TWO_OF_THREE, id=TWO_OF_THREE.key, marks=_XFAIL_TWO_OF_THREE),
        pytest.param(EMPTY_PARTIALS, id=EMPTY_PARTIALS.key),
        pytest.param(SHORT_A_LONG_B, id=SHORT_A_LONG_B.key),
        pytest.param(SHORT_BOTH, id=SHORT_BOTH.key),
        pytest.param(ALTERNATE_SHORT_B, id=ALTERNATE_SHORT_B.key, marks=_XFAIL_ALTERNATE_SHORT_B),
    ],
)
def test_broken_pair_withdrawn_within_10_requests(tmp_path, scenario):
    """Сломанная пара уходит с первого места живого поиска не позже 10-го запроса."""
    withdrawn_after = _requests_until_withdrawn(tmp_path, scenario)

    assert withdrawn_after is not None, f"{scenario.title}: не выведена за {MAX_REQUESTS} запросов"
    assert withdrawn_after <= LIMIT_REQUESTS, f"{scenario.title}: выведена после {withdrawn_after} запросов"


def test_breakdown_table_matches_report(tmp_path):
    """Таблица отчёта kt4_pair.md: через сколько запросов выведена пара в каждом сценарии."""
    scenarios = [ALL_FAIL, CAPTCHA_FIRST, ALTERNATE, TWO_OF_THREE, EMPTY_PARTIALS,
                 SHORT_A_LONG_B, SHORT_BOTH, ALTERNATE_SHORT_B]

    measured = {s.key: _requests_until_withdrawn(tmp_path / s.key, s) for s in scenarios}

    assert measured == FACTS


def test_short_history_pair_never_leads_while_second_phone_healthy(tmp_path):
    """Пара с короткой историей (3 сбора < speed_min_samples) не первая и до поломки, если B проверен."""
    pair = _make_pair(tmp_path, a_history=SHORT_HISTORY, b_history=HISTORY_OK)

    order = _live_order(pair, now=BREAK_TS)

    assert order == [HEALTHY, BROKEN]
    assert pair.table.get_stats(SITE_NAME, BROKEN, "live", now=BREAK_TS).n == SHORT_HISTORY


def _break_into_quarantine(pair: Pair) -> float:
    """Три провала подряд живого поиска; возвращает время последнего провала."""
    last_ts = BREAK_TS
    for k in range(1, 4):
        last_ts = BREAK_TS + k * REQUEST_STEP
        _record(pair.store, BROKEN, FAIL, ts=last_ts)
    return last_ts


def test_failed_trial_after_quarantine_puts_pair_back_into_quarantine(tmp_path):
    """После карантина 600 с проба фоном провалилась — пара сразу снова в карантине, люди её не получают."""
    pair = _make_pair(tmp_path)
    last_fail_ts = _break_into_quarantine(pair)
    assert _live_order(pair, now=last_fail_ts + CHECK_DELAY) == [HEALTHY, BROKEN]
    assert pair.table.get_pair_status(SITE_NAME, BROKEN, now=last_fail_ts + CHECK_DELAY).in_quarantine

    trial_ts = last_fail_ts + QUARANTINE_SECONDS + REQUEST_STEP
    pair.table.invalidate()
    waiting = pair.table.get_pair_status(SITE_NAME, BROKEN, now=trial_ts)
    assert (waiting.in_quarantine, waiting.awaiting_trial) == (False, True)
    assert _live_order(pair, now=trial_ts) == [HEALTHY, BROKEN]
    assert _bg_order(pair, now=trial_ts) == [BROKEN, HEALTHY]

    _record(pair.store, BROKEN, FAIL, ts=trial_ts, priority=2)
    after_ts = trial_ts + CHECK_DELAY
    live = _live_order(pair, now=after_ts)
    status = pair.table.get_pair_status(SITE_NAME, BROKEN, now=after_ts)

    assert live == [HEALTHY, BROKEN]
    assert _bg_order(pair, now=after_ts) == [HEALTHY, BROKEN]
    assert status.in_quarantine is True
    assert status.consecutive_failures == 4
    assert status.last_failure_ts == trial_ts


def test_failed_captcha_trial_after_cooldown_gives_3h_quarantine(tmp_path):
    """Капча → 1 ч; проба фоном снова поймала капчу → сразу карантин 3 ч."""
    pair = _make_pair(tmp_path)
    captcha_ts = BREAK_TS + REQUEST_STEP
    _record(pair.store, BROKEN, CAPTCHA, ts=captcha_ts)
    assert _live_order(pair, now=captcha_ts + CHECK_DELAY) == [HEALTHY, BROKEN]

    trial_ts = captcha_ts + CAPTCHA_FIRST_COOLDOWN + REQUEST_STEP
    assert _bg_order(pair, now=trial_ts) == [BROKEN, HEALTHY]
    _record(pair.store, BROKEN, CAPTCHA, ts=trial_ts, priority=2)
    after_ts = trial_ts + CHECK_DELAY
    live = _live_order(pair, now=after_ts)
    status = pair.table.get_pair_status(SITE_NAME, BROKEN, now=after_ts)

    assert live == [HEALTHY, BROKEN]
    assert status.in_quarantine is True
    assert status.consecutive_captchas == 2
    assert status.quarantine_seconds == CAPTCHA_SECOND_COOLDOWN
