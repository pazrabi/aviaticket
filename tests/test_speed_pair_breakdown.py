"""КТ4, работа 3 (П9): через сколько запросов `speed.py` владельца выводит сломанную пару «сайт × телефон».

Порог плана: пара, которая сломалась, выводится из живого поиска не позже чем через 10 запросов.
«Выведена» — `order_candidates(site, [A, B], priority=0)` не ставит A первой, хотя B здоров.

Запрос — строка `site_runs` пары A живого поиска (priority 0), как её пишут `droidhub._record_failure`
и `MetricsStore.record_results`. После каждой записи `droidhub` сбрасывает кэш таблицы (`invalidate`),
поэтому здесь таблица тоже пересчитывается после каждого запроса.

П35: флаг `SPEED_PAIR_WINDOW` — окно доли успехов пары (по умолчанию 30, как было) и, когда окно не 30,
сортировка запасных живого поиска: доказанно слабая пара — после непроверенной.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import random
from typing import Callable

import pytest

from avia.config import Settings, SiteConfig, load_settings
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


def _make_pair(
    tmp_path: Path, a_history: int = HISTORY_OK, b_history: int = HISTORY_OK, window: int | None = None
) -> Pair:
    """История за 7 дней: у A и B только удачные сборы, A быстрее. window — SPEED_PAIR_WINDOW (None — не задан)."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    start = BREAK_TS - 6 * DAY
    for i in range(a_history):
        _record(store, BROKEN, OK, ts=start + i * HISTORY_STEP, fetch=A_FETCH)
    for i in range(b_history):
        _record(store, HEALTHY, OK, ts=start + i * HISTORY_STEP + 1.0, fetch=B_FETCH)
    site = SiteConfig(name=SITE_NAME, url_template="https://test", phone=f"{BROKEN},{HEALTHY}")
    extra = {} if window is None else {"speed_pair_window": window}
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, **extra)
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


def _requests_until_withdrawn(tmp_path: Path, scenario: Scenario, window: int | None = None) -> int | None:
    """Номер запроса, после которого A больше не первая; 0 — не первая и до поломки; None — не выведена."""
    pair = _make_pair(tmp_path, a_history=scenario.a_history, b_history=scenario.b_history, window=window)
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

# П35, SPEED_PAIR_WINDOW = 15: окно 15 сборов и запасные «слабая после непроверенной».
WINDOW_FLAG = 15
FACTS_WINDOW_15: dict[str, int | None] = {
    ALL_FAIL.key: 3,
    CAPTCHA_FIRST.key: 1,
    ALTERNATE.key: 8,
    TWO_OF_THREE.key: 5,
    EMPTY_PARTIALS.key: 3,
    SHORT_A_LONG_B.key: 0,
    SHORT_BOTH.key: 2,
    ALTERNATE_SHORT_B.key: 8,
}
ALL_SCENARIOS = [ALL_FAIL, CAPTCHA_FIRST, ALTERNATE, TWO_OF_THREE, EMPTY_PARTIALS,
                 SHORT_A_LONG_B, SHORT_BOTH, ALTERNATE_SHORT_B]

_XFAIL_ALTERNATE = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "П9 не выполняется: «через раз» выведена только после 20 запросов (порог 10). Провалов подряд "
        "не больше 1 — карантина нет; доля успехов считается по последним 30 сборам пары, и wilson_lower "
        "падает ниже 0,6 только на 20 успехах из 30 (0,5806)"
    ),
)
_XFAIL_TWO_OF_THREE = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "П9 не выполняется: «два провала из трёх» выведена только после 14 запросов (порог 10). Провалов "
        "подряд не больше 2 — карантина нет; wilson_lower по последним 30 сборам ниже 0,6 только на 20 "
        "успехах из 30"
    ),
)
_XFAIL_ALTERNATE_SHORT_B = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
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
@pytest.mark.parametrize("window", [None, 30], ids=["window_unset", "window_30"])
def test_broken_pair_withdrawn_within_10_requests(tmp_path, scenario, window):
    """Сломанная пара уходит с первого места живого поиска не позже 10-го запроса (флаг не задан или 30)."""
    withdrawn_after = _requests_until_withdrawn(tmp_path, scenario, window=window)

    assert withdrawn_after is not None, f"{scenario.title}: не выведена за {MAX_REQUESTS} запросов"
    assert withdrawn_after <= LIMIT_REQUESTS, f"{scenario.title}: выведена после {withdrawn_after} запросов"


@pytest.mark.parametrize("scenario", [pytest.param(s, id=s.key) for s in ALL_SCENARIOS])
def test_broken_pair_withdrawn_within_10_requests_window_15(tmp_path, scenario):
    """SPEED_PAIR_WINDOW = 15: все сценарии, включая доп., выводятся не позже 10-го запроса."""
    withdrawn_after = _requests_until_withdrawn(tmp_path, scenario, window=WINDOW_FLAG)

    assert withdrawn_after is not None, f"{scenario.title}: не выведена за {MAX_REQUESTS} запросов"
    assert withdrawn_after <= LIMIT_REQUESTS, f"{scenario.title}: выведена после {withdrawn_after} запросов"


@pytest.mark.parametrize(
    ("window", "facts"),
    [(None, FACTS), (30, FACTS), (WINDOW_FLAG, FACTS_WINDOW_15)],
    ids=["window_unset", "window_30", "window_15"],
)
def test_breakdown_table_matches_report(tmp_path, window, facts):
    """Таблица отчёта kt4_pair.md: через сколько запросов выведена пара в каждом сценарии."""
    measured = {s.key: _requests_until_withdrawn(tmp_path / s.key, s, window=window) for s in ALL_SCENARIOS}

    assert measured == facts


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


# ---------------------------------------------------------------------------
# П35: флаг SPEED_PAIR_WINDOW
# ---------------------------------------------------------------------------


def _view(prefs: list[str]) -> tuple[list[str], float | None, int | None]:
    """Раскладка как её видит droidhub: порядок, терпение, сколько первых — «основные»."""
    return list(prefs), getattr(prefs, "patience", None), getattr(prefs, "preferred_count", None)


def _full_view(pair: Pair, now: float) -> tuple:
    """Всё, что speed.py отдаёт наружу по паре A/B: раскладки живого поиска и фона и метрики."""
    pair.table.invalidate()
    live = pair.table.order_candidates(pair.site, [BROKEN, HEALTHY], priority=0, now=now)
    bg = pair.table.order_candidates(pair.site, [BROKEN, HEALTHY], priority=2, now=now)
    stats = [pair.table.get_stats(SITE_NAME, dev, cls, now=now) for dev in (BROKEN, HEALTHY) for cls in ("live", "bg")]
    statuses = [pair.table.get_pair_status(SITE_NAME, dev, now=now) for dev in (BROKEN, HEALTHY)]
    return _view(live), _view(bg), stats, statuses


@pytest.mark.parametrize("scenario", [pytest.param(s, id=s.key) for s in (ALTERNATE_SHORT_B, TWO_OF_THREE, SHORT_BOTH)])
def test_window_unset_30_and_invalid_give_identical_routing(tmp_path, scenario):
    """Флаг не задан, 30 или меньше 5 — speed.py отдаёт то же, что и без флага, на каждом запросе."""
    traces: dict[str, list[tuple]] = {}
    for label, window in (("unset", None), ("30", 30), ("below_min", 4)):
        pair = _make_pair(tmp_path / label, a_history=scenario.a_history, b_history=scenario.b_history, window=window)
        trace = [_full_view(pair, BREAK_TS)]
        for k in range(1, 31):
            ts = BREAK_TS + k * REQUEST_STEP
            _record(pair.store, BROKEN, scenario.pattern(k), ts=ts)
            trace.append(_full_view(pair, ts + CHECK_DELAY))
        traces[label] = trace

    assert traces["30"] == traces["unset"]
    assert traces["below_min"] == traces["unset"]


WEAK = "A"  # доказанно слабая: 30 сборов через раз
UNPROVEN = "B"  # непроверенная: 3 сбора < speed_min_samples
QUARANTINED = "C"  # 3 провала подряд только что
AWAITING = "D"  # 3 провала подряд давно — ждёт пробы
NOW = BREAK_TS


def _backup_sort_table(tmp_path: Path, window: int | None) -> tuple[SpeedTable, SiteConfig]:
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    start = NOW - 6 * DAY
    for i in range(HISTORY_OK):
        _record(store, QUARANTINED, OK, ts=start + i * HISTORY_STEP, fetch=B_FETCH)
        _record(store, AWAITING, OK, ts=start + i * HISTORY_STEP + 1.0, fetch=B_FETCH)
    for i in range(SHORT_HISTORY):
        _record(store, UNPROVEN, OK, ts=start + i * HISTORY_STEP + 2.0, fetch=B_FETCH)
    for i in range(30):
        _record(store, WEAK, OK if i % 2 == 0 else FAIL, ts=NOW - 3 * 3600.0 + i * REQUEST_STEP)
    for i in range(3):
        _record(store, QUARANTINED, FAIL, ts=NOW - 120.0 + i * 30.0)
        _record(store, AWAITING, FAIL, ts=NOW - 2 * 3600.0 + i * 30.0)
    site = SiteConfig(name=SITE_NAME, url_template="https://test", phone="A,B,C,D")
    extra = {} if window is None else {"speed_pair_window": window}
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, **extra)
    return SpeedTable(metrics_path=db_path, settings=settings), site


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (None, [WEAK, UNPROVEN, QUARANTINED, AWAITING]),
        (30, [WEAK, UNPROVEN, QUARANTINED, AWAITING]),
        (WINDOW_FLAG, [UNPROVEN, WEAK, QUARANTINED, AWAITING]),
        (20, [UNPROVEN, WEAK, QUARANTINED, AWAITING]),
    ],
    ids=["window_unset", "window_30", "window_15", "window_20"],
)
def test_live_backups_weak_after_unproven_only_under_flag(tmp_path, window, expected):
    """Флаг задан и не 30: доказанно слабая пара — после непроверенной; карантин и ожидание пробы — в конце."""
    table, site = _backup_sort_table(tmp_path, window)
    weak = table.get_stats(SITE_NAME, WEAK, "live", now=NOW)
    unproven = table.get_stats(SITE_NAME, UNPROVEN, "live", now=NOW)
    assert weak.n >= 5 and weak.p_lower < 0.6
    assert unproven.n < 5
    assert weak.cost < unproven.cost  # без флага слабая впереди только по цене C
    assert table.get_pair_status(SITE_NAME, QUARANTINED, now=NOW).in_quarantine
    assert table.get_pair_status(SITE_NAME, AWAITING, now=NOW).awaiting_trial

    order = table.order_candidates(site, [WEAK, UNPROVEN, QUARANTINED, AWAITING], priority=0, now=NOW)

    assert list(order) == expected


def test_backup_sort_does_not_touch_subscriptions_and_background(tmp_path):
    """Сортировка запасных — только живой поиск: раскладка фона та же при окне 30 и 15."""
    bg_orders = {}
    for window in (None, WINDOW_FLAG):
        table, site = _backup_sort_table(tmp_path / str(window), window)
        bg_orders[window] = _view(
            table.order_candidates(site, [WEAK, UNPROVEN, QUARANTINED, AWAITING], priority=2, now=NOW)
        )

    assert bg_orders[WINDOW_FLAG] == bg_orders[None]


# Цена окна 15: здоровая пара с 90 % успехов. Исходы A — случайные, seed фиксирован; A пробуют на каждом
# запросе, как в модели отчёта работы 3. «Не первая» — B первой, хотя A здорова.
HEALTHY_OK_RATE = 0.9
PRICE_REQUESTS = 1000  # 16,7 ч по минуте: история B (50 сборов) не выходит из окна 7 дней
PRICE_SEED = 7
PRICE_LIMIT_SHARE = {30: 0.01, WINDOW_FLAG: 0.10}


def _healthy_outcomes(count: int, ok_rate: float, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [OK if rng.random() < ok_rate else FAIL for _ in range(count)]


def _share_not_first(tmp_path: Path, window: int, outcomes: list[str]) -> tuple[int, float]:
    pair = _make_pair(tmp_path, window=window)
    assert _live_order(pair, now=BREAK_TS)[0] == BROKEN
    not_first = 0
    for k, kind in enumerate(outcomes, start=1):
        ts = BREAK_TS + k * REQUEST_STEP
        _record(pair.store, BROKEN, kind, ts=ts)
        if _live_order(pair, now=ts + CHECK_DELAY)[0] != BROKEN:
            not_first += 1
    return not_first, not_first / len(outcomes)


def test_price_healthy_pair_90_percent_not_first_share(tmp_path):
    """Цена окна 15: доля запросов, на которых здоровая пара (90 % успехов) не первая, — в пределах границы."""
    outcomes = _healthy_outcomes(PRICE_REQUESTS, HEALTHY_OK_RATE, PRICE_SEED)

    shares = {w: _share_not_first(tmp_path / f"w{w}", w, outcomes)[1] for w in PRICE_LIMIT_SHARE}

    assert shares[30] <= PRICE_LIMIT_SHARE[30], shares
    assert shares[WINDOW_FLAG] <= PRICE_LIMIT_SHARE[WINDOW_FLAG], shares
    assert shares[WINDOW_FLAG] >= shares[30], shares


def _bg_change(tmp_path: Path, scenario: Scenario, window: int | None) -> tuple[int | None, tuple, tuple]:
    """Номер запроса, после которого раскладка фона для [A, B] меняется; раскладка до и после."""
    pair = _make_pair(tmp_path, a_history=scenario.a_history, b_history=scenario.b_history, window=window)
    before = _view(_bg_prefs(pair, now=BREAK_TS))
    for k in range(1, MAX_REQUESTS + 1):
        ts = BREAK_TS + k * REQUEST_STEP
        _record(pair.store, BROKEN, scenario.pattern(k), ts=ts)
        after = _view(_bg_prefs(pair, now=ts + CHECK_DELAY))
        if after != before:
            return k, before, after
    return None, before, before


def _bg_prefs(pair: Pair, now: float) -> list[str]:
    pair.table.invalidate()
    return pair.table.order_candidates(pair.site, [BROKEN, HEALTHY], priority=2, now=now)


@pytest.mark.parametrize(
    ("window", "changed_after"), [(None, 20), (WINDOW_FLAG, 8)], ids=["window_unset", "window_15"]
)
def test_background_order_alternate_pair_window_15(tmp_path, window, changed_after):
    """Подписки и фон: «через раз» A уходит в ненадёжные (последней, без терпения 60 с) раньше при окне 15."""
    k, before, after = _bg_change(tmp_path, ALTERNATE, window)

    assert before == ([HEALTHY, BROKEN], 60.0, 1)  # медленная B первой, быстрая A — после 60 с ожидания
    assert (k, after) == (changed_after, ([HEALTHY, BROKEN], 0.0, 2))


def test_settings_speed_pair_window_default():
    assert Settings(sites=[]).speed_pair_window == 30


@pytest.mark.parametrize(
    ("env", "expected"),
    [(None, 30), ("30", 30), ("15", 15), ("5", 5), (" 20 ", 20), ("4", 30), ("0", 30), ("-15", 30),
     ("abc", 30), ("", 30), ("15.5", 30)],
)
def test_load_settings_speed_pair_window(monkeypatch, env, expected):
    """SPEED_PAIR_WINDOW: целое; мусор или меньше 5 — 30."""
    if env is None:
        monkeypatch.delenv("SPEED_PAIR_WINDOW", raising=False)
    else:
        monkeypatch.setenv("SPEED_PAIR_WINDOW", env)

    assert load_settings().speed_pair_window == expected
