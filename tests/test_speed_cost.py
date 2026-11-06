"""Тесты нового расчета скорости C, нижней границы Уилсона, множителя телефона и кулдаунов."""
from __future__ import annotations

import time

import pytest

from avia.config import Settings, SiteConfig
from avia.metrics import MetricsStore
from avia.speed import SpeedTable, get_speed_table, reset_speed_tables, wilson_lower


@pytest.fixture(autouse=True)
def clean_speed_caches():
    reset_speed_tables()
    yield
    reset_speed_tables()


# ---------------------------------------------------------------------------
# 1. Формула C: ожидаемое время до успеха и порядок кандидатов
# ---------------------------------------------------------------------------


def test_formula_c_and_candidate_order(tmp_path):
    """Формула C на примере:
    - пара 25 с, 95% успеха ≈ 27.5 с;
    - пара 15 с, 60% успеха, провалы на 30-й с ≈ 35.8 с.
    Порядок: первая лучше второй, несмотря на то что у второй медиана успешных 15 с < 25 с.
    """
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # phone_stable: 20 запусков, 19 ok по 25.0 с, 1 провал (502) на 25.0 с (95% успеха)
    # p = (19+1)/(20+2) = 20/22 = 10/11. t_ok = 25.0, t_fail = 25.0.
    # C = (p*25 + (1-p)*25) / p = 25 / (10/11) = 27.5 с
    for i in range(19):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=25.0, ts=t0 + i, device="phone_stable", fetch_seconds=25.0, priority=0)
    store.record_run("user", "as", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=25.0, ts=t0 + 19, device="phone_stable", fetch_seconds=25.0, priority=0)

    # phone_flaky: 20 запусков, 12 ok по 15.0 с, 8 провалов на 30.0 с (60% успеха)
    # p = (12+1)/(20+2) = 13/22. t_ok = 15.0, t_fail = 30.0.
    # C = (13/22*15 + 9/22*30) / (13/22) = (195 + 270) / 13 = 465 / 13 ≈ 35.77 с
    for i in range(12):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=15.0, ts=t0 + i, device="phone_flaky", fetch_seconds=15.0, priority=0)
    for i in range(12, 20):
        store.record_run("user", "as", ok=False, partial=False, captcha=False, error="timeout",
                         offers=0, elapsed=30.0, ts=t0 + i, device="phone_flaky", fetch_seconds=30.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="phone_flaky,phone_stable")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    st_stable = table.get_stats("as", "phone_stable", "live")
    st_flaky = table.get_stats("as", "phone_flaky", "live")

    # Честная медиана успешных запусков в median_fetch
    assert st_stable.median_fetch == 25.0
    assert st_flaky.median_fetch == 15.0

    # Расчетная стоимость C в поле cost
    assert st_stable.cost == 27.5
    assert st_flaky.cost == 35.77
    assert st_stable.cost < st_flaky.cost

    # Для живого поиска (priority 0) порядок кандидатов строится по возрастанию C:
    # phone_stable (27.5 с) идет первым, phone_flaky (35.77 с) — вторым
    cands_live = table.order_candidates(site, ["phone_flaky", "phone_stable"], priority=0, now=t0 + 50)
    assert list(cands_live) == ["phone_stable", "phone_flaky"]


# ---------------------------------------------------------------------------
# 2. Нижняя граница Уилсона не пускает пару 2 из 2 в основные
# ---------------------------------------------------------------------------


def test_wilson_lower_bound_blocks_two_of_two(tmp_path):
    """Пара с 2 из 2 успешных запусков имеет p_lower < 0.6 и не допускается в основные."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # lucky_phone: 2 запуска, оба ok по 2.0 с (быстрый, но мало данных)
    # p = (2+1)/(2+2) = 0.75, p_lower ≈ 0.454 < 0.6
    for i in range(2):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=2.0, ts=t0 + i, device="lucky_phone", fetch_seconds=2.0, priority=0)

    # veteran_phone: 6 запусков, 6 ok по 5.0 с (проверенный телефон)
    # p = (6+1)/(6+2) = 0.875, p_lower ≈ 0.721 >= 0.6
    for i in range(6):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=5.0, ts=t0 + i, device="veteran_phone", fetch_seconds=5.0, priority=0)

    site = SiteConfig(name="as", url_template="https://test", phone="lucky_phone,veteran_phone")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, speed_live_min_p_lower=0.6)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    st_lucky = table.get_stats("as", "lucky_phone", "live")
    assert st_lucky.n == 2
    assert st_lucky.ok_count == 2
    assert st_lucky.p_lower < 0.6

    st_vet = table.get_stats("as", "veteran_phone", "live")
    assert st_vet.n == 6
    assert st_vet.p_lower >= 0.6

    # В живом поиске lucky_phone не попадает в основные, а остается запасным
    cands_live = table.order_candidates(site, ["lucky_phone", "veteran_phone"], priority=0, now=t0 + 50)
    assert list(cands_live) == ["veteran_phone", "lucky_phone"]
    assert cands_live.preferred_count == 1  # Только veteran_phone — основной


# ---------------------------------------------------------------------------
# 3. Множитель телефона для новой пары
# ---------------------------------------------------------------------------


def test_phone_multiplier_for_new_pair(tmp_path):
    """Новая пара без данных оценивается как: медиана сайта * множитель скорости телефона.
    Множитель телефона = медиана по сайтам отношений (медиана телефона / медиана сайта).
    """
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # site_a: медиана 10.0 с
    # phone_fast: 5.0 с (ratio = 0.5)
    # phone_slow: 15.0 с (ratio = 1.5)
    for i in range(6):
        store.record_run("user", "site_a", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=5.0, ts=t0 + i, device="phone_fast", fetch_seconds=5.0, priority=0)
        store.record_run("user", "site_a", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=15.0, ts=t0 + i, device="phone_slow", fetch_seconds=15.0, priority=0)

    # site_b: медиана 20.0 с
    # phone_fast: 10.0 с (ratio = 0.5)
    # phone_slow: 30.0 с (ratio = 1.5)
    for i in range(6):
        store.record_run("user", "site_b", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=10.0, ts=t0 + i, device="phone_fast", fetch_seconds=10.0, priority=0)
        store.record_run("user", "site_b", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=30.0, ts=t0 + i, device="phone_slow", fetch_seconds=30.0, priority=0)

    # site_target: медиана 40.0 с по замерам другого телефона phone_other
    for i in range(6):
        store.record_run("user", "site_target", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=40.0, ts=t0 + i, device="phone_other", fetch_seconds=40.0, priority=0)

    # phone_fast, phone_slow и phone_brand_new не имеют замеров на site_target (n=0)
    site = SiteConfig(name="site_target", url_template="https://test", phone="phone_fast,phone_slow,phone_brand_new")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    st_fast = table.get_stats("site_target", "phone_fast", "live")
    assert st_fast.n == 0
    # 40.0 * 0.5 = 20.0 с
    assert st_fast.cost == 20.0

    st_slow = table.get_stats("site_target", "phone_slow", "live")
    assert st_slow.n == 0
    # 40.0 * 1.5 = 60.0 с
    assert st_slow.cost == 60.0

    st_new = table.get_stats("site_target", "phone_brand_new", "live")
    assert st_new.n == 0
    # Данных нет совсем — множитель 1.0 -> 40.0 с
    assert st_new.cost == 40.0

    # В фоновом классе (priority > 0) новые пары исследуются первыми
    cands_bg = table.order_candidates(site, ["phone_other", "phone_fast", "phone_slow", "phone_brand_new"], priority=2, now=t0 + 50)
    # phone_other имеет n=6, поэтому идет после новых исследовательских пар
    assert list(cands_bg)[:3] == ["phone_fast", "phone_slow", "phone_brand_new"]
    assert list(cands_bg)[3] == "phone_other"


# ---------------------------------------------------------------------------
# 4. Карантин по типу провала: капча 1 ч / 3 ч / 6 ч / 12 ч
# ---------------------------------------------------------------------------


def test_captcha_cooldown_levels(tmp_path):
    """Капча вызывает кулдаун 1 ч, при повторной 3 ч, затем 6 ч, 12 ч (макс 12 ч)."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    site = SiteConfig(name="as", url_template="https://test", phone="dev_c")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)

    # 1-я капча в t=1000 -> кулдаун 1 ч (3600 с)
    store.record_run("user", "as", ok=False, partial=False, captcha=True, error="captcha",
                     offers=0, elapsed=1.0, ts=1000.0, device="dev_c", priority=0)
    table.refresh(now=1000.0 + 1800.0)  # 30 мин
    pst1 = table.get_pair_status("as", "dev_c", now=1000.0 + 1800.0)
    assert pst1.in_quarantine is True
    assert pst1.awaiting_trial is False

    table.refresh(now=1000.0 + 3601.0)  # 1 ч 1 с
    pst1_exit = table.get_pair_status("as", "dev_c", now=1000.0 + 3601.0)
    assert pst1_exit.in_quarantine is False
    assert pst1_exit.awaiting_trial is True

    # 2-я капча при пробной попытке в t=4602 -> кулдаун 3 ч (10800 с)
    store.record_run("bg", "as", ok=False, partial=False, captcha=True, error="captcha",
                     offers=0, elapsed=1.0, ts=4602.0, device="dev_c", priority=2)
    table.refresh(now=4602.0 + 7200.0)  # 2 ч
    pst2 = table.get_pair_status("as", "dev_c", now=4602.0 + 7200.0)
    assert pst2.in_quarantine is True
    assert pst2.awaiting_trial is False

    table.refresh(now=4602.0 + 10801.0)  # 3 ч 1 с
    pst2_exit = table.get_pair_status("as", "dev_c", now=4602.0 + 10801.0)
    assert pst2_exit.in_quarantine is False
    assert pst2_exit.awaiting_trial is True

    # 3-я капча в t=15404 -> кулдаун 6 ч (21600 с)
    store.record_run("bg", "as", ok=False, partial=False, captcha=True, error="captcha",
                     offers=0, elapsed=1.0, ts=15404.0, device="dev_c", priority=2)
    table.refresh(now=15404.0 + 18000.0)  # 5 ч
    pst3 = table.get_pair_status("as", "dev_c", now=15404.0 + 18000.0)
    assert pst3.in_quarantine is True
    assert pst3.awaiting_trial is False

    table.refresh(now=15404.0 + 21601.0)  # 6 ч 1 с
    pst3_exit = table.get_pair_status("as", "dev_c", now=15404.0 + 21601.0)
    assert pst3_exit.in_quarantine is False
    assert pst3_exit.awaiting_trial is True

    # 4-я капча в t=37006 -> кулдаун 12 ч (43200 с)
    store.record_run("bg", "as", ok=False, partial=False, captcha=True, error="captcha",
                     offers=0, elapsed=1.0, ts=37006.0, device="dev_c", priority=2)
    table.refresh(now=37006.0 + 36000.0)  # 10 ч
    pst4 = table.get_pair_status("as", "dev_c", now=37006.0 + 36000.0)
    assert pst4.in_quarantine is True
    assert pst4.awaiting_trial is False

    table.refresh(now=37006.0 + 43201.0)  # 12 ч 1 с
    pst4_exit = table.get_pair_status("as", "dev_c", now=37006.0 + 43201.0)
    assert pst4_exit.in_quarantine is False
    assert pst4_exit.awaiting_trial is True

    # 5-я капча в t=80208 -> максимум 12 ч (43200 с)
    store.record_run("bg", "as", ok=False, partial=False, captcha=True, error="captcha",
                     offers=0, elapsed=1.0, ts=80208.0, device="dev_c", priority=2)
    table.refresh(now=80208.0 + 43201.0)  # 12 ч 1 с
    pst5_exit = table.get_pair_status("as", "dev_c", now=80208.0 + 43201.0)
    assert pst5_exit.in_quarantine is False
    assert pst5_exit.awaiting_trial is True

    # Успешный запуск снимает карантин
    store.record_run("bg", "as", ok=True, partial=False, captcha=False, error=None,
                     offers=5, elapsed=3.0, ts=123410.0, device="dev_c", priority=2)
    table.refresh(now=123415.0)
    pst_ok = table.get_pair_status("as", "dev_c", now=123415.0)
    assert pst_ok.consecutive_failures == 0
    assert pst_ok.in_quarantine is False
    assert pst_ok.awaiting_trial is False


# ---------------------------------------------------------------------------
# 5. Карантин 502/таймаут: 3 провала подряд, кулдаун 10 минут
# ---------------------------------------------------------------------------


def test_502_cooldown_10_minutes(tmp_path):
    """502/таймаут требует 3 провала подряд и дает кулдаун 10 минут."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    site = SiteConfig(name="as", url_template="https://test", phone="dev_502")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, speed_quarantine_seconds=600)
    table = get_speed_table(settings)

    # 1-й провал 502
    store.record_run("user", "as", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=1.0, ts=1000.0, device="dev_502", priority=0)
    table.refresh(now=1005.0)
    assert table.get_pair_status("as", "dev_502", now=1005.0).in_quarantine is False

    # 2-й провал 502
    store.record_run("user", "as", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=1.0, ts=1010.0, device="dev_502", priority=0)
    table.refresh(now=1015.0)
    assert table.get_pair_status("as", "dev_502", now=1015.0).in_quarantine is False

    # 3-й провал 502 в t=1020 -> карантин на 10 минут (600 с)
    store.record_run("user", "as", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=1.0, ts=1020.0, device="dev_502", priority=0)

    # Спустя 5 минут (t=1320 < 1020 + 600 = 1620) — активный карантин
    table.refresh(now=1320.0)
    pst = table.get_pair_status("as", "dev_502", now=1320.0)
    assert pst.in_quarantine is True
    assert pst.awaiting_trial is False

    # Спустя 11 минут (t=1680 > 1620) — кулдаун истек, ждет пробной попытки
    table.refresh(now=1680.0)
    pst_exit = table.get_pair_status("as", "dev_502", now=1680.0)
    assert pst_exit.in_quarantine is False
    assert pst_exit.awaiting_trial is True


# ---------------------------------------------------------------------------
# 6. Неудачная проба после капчи (502 / ошибка) не выпускает пару из ожидания пробы
# ---------------------------------------------------------------------------


def test_captcha_trial_failure_non_captcha_does_not_release_pair(tmp_path):
    """Провал пробной попытки (502/таймаут) после кулдауна капчи не снимает статус ожидания пробы."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    site = SiteConfig(name="as", url_template="https://test", phone="dev_trial,dev_other")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store, speed_quarantine_seconds=600)
    table = get_speed_table(settings)

    # Капча в t=1000
    store.record_run("user", "as", ok=False, partial=False, captcha=True, error="captcha",
                     offers=0, elapsed=1.0, ts=1000.0, device="dev_trial", priority=0)
    # dev_other стабильный
    for i in range(5):
        store.record_run("user", "as", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=3.0, ts=1000.0 + i, device="dev_other", priority=0)

    # Через 1 ч 1 с (t=4601) кулдаун истек -> awaiting_trial
    table.refresh(now=4601.0)
    pst = table.get_pair_status("as", "dev_trial", now=4601.0)
    assert pst.in_quarantine is False
    assert pst.awaiting_trial is True

    # Фоновый процесс делает пробный запрос в t=4602, но получает 502 (не капчу)
    store.record_run("bg", "as", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=1.0, ts=4602.0, device="dev_trial", priority=2)

    # В t=4605 статус пары НЕ должен сброситься в здоровый: она остается awaiting_trial!
    table.refresh(now=4605.0)
    pst_after_trial_fail = table.get_pair_status("as", "dev_trial", now=4605.0)
    assert pst_after_trial_fail.in_quarantine is False
    assert pst_after_trial_fail.awaiting_trial is True

    # Для живого поиска (priority 0) dev_trial остается недоступным (в bad_candidates)
    cands_live = table.order_candidates(site, ["dev_trial", "dev_other"], priority=0, now=4605.0)
    assert list(cands_live) == ["dev_other", "dev_trial"]
    assert cands_live.preferred_count == 1

    # Фон направляется на повторную пробу
    cands_bg = table.order_candidates(site, ["dev_other", "dev_trial"], priority=2, now=4605.0)
    assert list(cands_bg)[0] == "dev_trial"

    # Следующая проба успешна в t=4610 -> пара полностью восстанавливается
    store.record_run("bg", "as", ok=True, partial=False, captcha=False, error=None,
                     offers=5, elapsed=2.0, ts=4610.0, device="dev_trial", priority=2)
    table.refresh(now=4615.0)
    pst_recovered = table.get_pair_status("as", "dev_trial", now=4615.0)
    assert pst_recovered.in_quarantine is False
    assert pst_recovered.awaiting_trial is False


# ---------------------------------------------------------------------------
# 7. Новые пары без данных в живом поиске сортируются по C и возвращают PreferenceList
# ---------------------------------------------------------------------------


def test_new_phones_live_search_sorted_by_cost_and_returns_preference_list(tmp_path):
    """Новые пары (n=0) в живом поиске упорядочиваются по возрастанию C, возвращая PreferenceList."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # На site_src: phone_fast имеет медиану 5с (ratio=0.5), phone_slow — 20с (ratio=2.0)
    for i in range(6):
        store.record_run("user", "site_src", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=5.0, ts=t0 + i, device="phone_fast", fetch_seconds=5.0, priority=0)
        store.record_run("user", "site_src", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=20.0, ts=t0 + i, device="phone_slow", fetch_seconds=20.0, priority=0)
        store.record_run("user", "site_target", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=10.0, ts=t0 + i, device="phone_anchor", fetch_seconds=10.0, priority=0)

    # На site_target у phone_fast, phone_mid и phone_slow нет замеров (n=0)
    site = SiteConfig(name="site_target", url_template="https://test", phone="phone_slow,phone_fast,phone_mid")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    # Кандидаты передаются обычным списком list
    raw_candidates = ["phone_slow", "phone_fast", "phone_mid"]
    res = table.order_candidates(site, raw_candidates, priority=0, now=t0 + 50)

    # Должен возвращаться именно PreferenceList
    from avia.droidhub import PreferenceList
    assert isinstance(res, PreferenceList)
    assert res.preferred_count == 1
    # Порядок должен быть по возрастанию C:
    # phone_fast (10 * 0.5 = 5.0 с) -> phone_mid (10 * 1.0 = 10.0 с) -> phone_slow (10 * 2.0 = 20.0 с)
    assert list(res) == ["phone_fast", "phone_mid", "phone_slow"]


# ---------------------------------------------------------------------------
# 8. SpeedStats хранит честную median_fetch, а get_site_median и админка работают согласованно
# ---------------------------------------------------------------------------


def test_speed_stats_honest_median_fetch_and_admin_panel(tmp_path):
    """SpeedStats.median_fetch хранит фактическую медиану секунд, а админка показывает C."""
    from avia.admin_panel import build_phone_order

    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # 19 успехов по 20.0 с, 1 провал на 40.0 с -> median_fetch = 20.0 с, cost ≈ 23.64 с
    for i in range(19):
        store.record_run("user", "site_test", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=20.0, ts=t0 + i, device="dev_a", fetch_seconds=20.0, priority=0)
    store.record_run("user", "site_test", ok=False, partial=False, captcha=False, error="502",
                     offers=0, elapsed=40.0, ts=t0 + 19, device="dev_a", fetch_seconds=40.0, priority=0)

    for i in range(5):
        store.record_run("user", "site_test", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=30.0, ts=t0 + i, device="dev_b", fetch_seconds=30.0, priority=0)

    site = SiteConfig(name="site_test", url_template="https://test", phone="dev_a,dev_b")
    settings = Settings(sites=[site], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    st_a = table.get_stats("site_test", "dev_a", "live")
    # Честная медиана успешных
    assert st_a.median_fetch == 20.0
    # C отличается от медианы
    assert st_a.cost is not None
    assert st_a.cost > st_a.median_fetch

    # get_site_median с exclude_phone="dev_b" считает медиану по честным median_fetch оставшихся телефонов
    med_ex = table.get_site_median("site_test", "live", exclude_phone="dev_b")
    assert med_ex == 20.0

    # Админ-панель build_phone_order отображает C вместо median_fetch
    ctx = {
        "settings": settings,
        "summary": store.summary(0.0),
        "devices_summary": store.summary_by_device(0.0),
        "site_devices_summary": store.summary_by_site_device(0.0),
        "counts_by_source": {},
        "recent_errors": [],
    }
    text = build_phone_order(ctx)
    assert f"{st_a.cost:.1f} с" in text


# ---------------------------------------------------------------------------
# 9. Единый множитель телефона учитывает фоновые замеры для живого поиска
# ---------------------------------------------------------------------------


def test_unified_phone_multiplier_across_classes(tmp_path):
    """Множитель телефона вычисляется единообразно по всем данным телефона и сайтов."""
    db_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(db_path)
    t0 = time.time() - 3600.0

    # phone_bg_only имеет замеры ТОЛЬКО в фоне (priority=2) на site_1
    # site_1: dev_fast (phone_bg_only) 10.0 с, dev_slow 30.0 с -> медиана сайта по всем телефонам 20.0 с
    # ratio для phone_bg_only = 10.0 / 20.0 = 0.5
    for i in range(6):
        store.record_run("bg", "site_1", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=30.0, ts=t0 + i, device="dev_slow", fetch_seconds=30.0, priority=2)
        store.record_run("bg", "site_1", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=10.0, ts=t0 + i, device="phone_bg_only", fetch_seconds=10.0, priority=2)

    # site_2: замеры только dev_other (медиана 40.0 с)
    for i in range(6):
        store.record_run("user", "site_2", ok=True, partial=False, captcha=False, error=None,
                         offers=5, elapsed=40.0, ts=t0 + i, device="dev_other", fetch_seconds=40.0, priority=0)

    site2 = SiteConfig(name="site_2", url_template="https://test", phone="phone_bg_only")
    settings = Settings(sites=[site2], storage_dir=tmp_path, metrics=store)
    table = get_speed_table(settings)
    table.refresh(now=t0 + 50)

    # Множитель телефона единый
    mult = table.get_phone_multiplier("phone_bg_only")
    assert mult == 0.5

    # Для живого поиска (live) на site_2 ожидаемая оценка C = 40.0 * 0.5 = 20.0 с
    st_live = table.get_stats("site_2", "phone_bg_only", "live")
    assert st_live.n == 0
    assert st_live.cost == 20.0

