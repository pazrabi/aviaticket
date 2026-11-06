"""Тесты стадий сайтов trial/live."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from avia import admin_panel
from avia.bot import Bot
from avia.config import Settings, SiteConfig, load_settings
from avia.metrics import MetricsStore
from avia.criteria import SearchCriteria
from avia.models import Offer, Query, SiteResult
from avia.pipeline import search
from avia.scheduler import Scheduler
from avia.stage import TRIAL_MIN_OK_SHARE, TRIAL_MIN_PHONES, TRIAL_MIN_RUNS, trial_ready, trial_stats
from avia.subscriptions import Subscription, SubscriptionStore


def _make_dummy_query() -> Query:
    return Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))


def _make_offer(site: str, price: int) -> Offer:
    return Offer(site=site, price=price, currency="RUB")


def test_unknown_stage_fallback_to_live(caplog):
    """Неизвестный stage сбрасывается в 'live' с предупреждением в лог."""
    with caplog.at_level(logging.WARNING, logger="avia.config"):
        cfg = SiteConfig(
            name="experimental",
            url_template="http://example.com",
            stage="some_weird_stage",  # type: ignore[arg-type]
        )
    assert cfg.stage == "live"
    assert any("Неизвестное значение stage" in r.message for r in caplog.records)


def test_trial_site_not_queried_in_live_search():
    """Trial-сайт не запрашивается в живом поиске (priority=0) и не попадает в результаты."""
    live_site = SiteConfig(name="live_site", url_template="http://example.com/live", stage="live")
    trial_site = SiteConfig(name="trial_site", url_template="http://example.com/trial", stage="trial")
    settings = Settings(sites=[live_site, trial_site], trial_stage=True)

    queried_sites: list[str] = []

    async def fake_run_site(site, *args, **kwargs):
        queried_sites.append(site.name)
        return SiteResult(site=site.name, ok=True, offers=[_make_offer(site.name, 1000)])

    with patch("avia.pipeline._run_site", side_effect=fake_run_site):
        results = asyncio.run(search(_make_dummy_query(), settings, priority=0))

    assert queried_sites == ["live_site"]
    assert len(results) == 1
    assert results[0].site == "live_site"
    # trial_site не показывается как недоступен или ошибка
    assert not any(r.site == "trial_site" for r in results)


def test_trial_site_not_queried_in_deep_search():
    """Trial-сайт не запрашивается в глубоком живом поиске (priority=0, deep=True)."""
    live_site = SiteConfig(name="live_site", url_template="http://example.com/live", stage="live")
    trial_site = SiteConfig(name="trial_site", url_template="http://example.com/trial", stage="trial")
    settings = Settings(sites=[live_site, trial_site], trial_stage=True)

    queried_sites: list[str] = []

    async def fake_run_site(site, *args, **kwargs):
        queried_sites.append(site.name)
        return SiteResult(site=site.name, ok=True, offers=[_make_offer(site.name, 1000)])

    with patch("avia.pipeline._run_site", side_effect=fake_run_site):
        results = asyncio.run(search(_make_dummy_query(), settings, priority=0, deep=True))

    assert queried_sites == ["live_site"]
    assert len(results) == 1
    assert results[0].site == "live_site"


def test_trial_site_queried_in_background():
    """Trial-сайт запрашивается как обычно для priority >= 1 (подписки, фон)."""
    live_site = SiteConfig(name="live_site", url_template="http://example.com/live", stage="live")
    trial_site = SiteConfig(name="trial_site", url_template="http://example.com/trial", stage="trial")
    settings = Settings(sites=[live_site, trial_site], trial_stage=True)

    queried_sites: list[str] = []

    async def fake_run_site(site, *args, **kwargs):
        queried_sites.append(site.name)
        return SiteResult(site=site.name, ok=True, offers=[_make_offer(site.name, 1000)])

    with patch("avia.pipeline._run_site", side_effect=fake_run_site):
        # Подписочный поиск (priority=1)
        results_sub = asyncio.run(search(_make_dummy_query(), settings, priority=1))

    assert set(queried_sites) == {"live_site", "trial_site"}
    assert {r.site for r in results_sub} == {"live_site", "trial_site"}

    queried_sites.clear()
    with patch("avia.pipeline._run_site", side_effect=fake_run_site):
        # Фоновый сбор цен (priority=2)
        results_bg = asyncio.run(search(_make_dummy_query(), settings, priority=2))

    assert set(queried_sites) == {"live_site", "trial_site"}
    assert {r.site for r in results_bg} == {"live_site", "trial_site"}


def test_trial_site_excluded_from_subscription_notifications(tmp_path):
    """Офферы trial-сайта отбрасываются в scheduler при выборе лучшего оффера."""
    store = SubscriptionStore(tmp_path / "subs.sqlite")
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    sub = store.create(
        chat_id=101,
        query=q,
        baggage_mode="any",
        criteria=SearchCriteria(),
        now=1000.0,
    )
    # Первый проход записывает baseline
    store.record_price(sub.id, 3000, "RUB", None, 1000.0)

    # Поиск возвращает дешёвый оффер trial-сайта (1000) и более дорогой live-сайта (2500)
    async def fake_search(query):
        return [
            SiteResult(site="trial_site", ok=True, offers=[_make_offer("trial_site", 1000)]),
            SiteResult(site="live_site", ok=True, offers=[_make_offer("live_site", 2500)]),
        ]

    notifications: list[tuple[int, str, dict]] = []

    async def fake_notify(chat_id, text, reply_markup=None):
        notifications.append((chat_id, text, reply_markup))

    sched = Scheduler(
        store=store,
        search_fn=fake_search,
        notify_fn=fake_notify,
        clock=lambda: 2000.0,
        today=lambda: date(2026, 11, 20),
        trial_sites={"trial_site"},
    )

    sent = asyncio.run(sched.run_once())
    assert sent == 1
    assert len(notifications) == 1
    # Уведомление отправлено с предложением live-сайта (2 500 ₽), а не trial-сайта (1 000 ₽)
    msg_text = notifications[0][1]
    assert "live_site" in msg_text
    assert "trial_site" not in msg_text
    assert "500" in msg_text
    assert "1 000" not in msg_text and "1\xa0000" not in msg_text


def test_trial_site_only_no_subscription_notification(tmp_path):
    """Если офферы есть только у trial-сайта, уведомление подписки не отправляется."""
    store = SubscriptionStore(tmp_path / "subs.sqlite")
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))
    sub = store.create(
        chat_id=102,
        query=q,
        baggage_mode="any",
        criteria=SearchCriteria(),
        now=1000.0,
    )
    store.record_price(sub.id, 3000, "RUB", None, 1000.0)

    async def fake_search(query):
        return [
            SiteResult(site="trial_site", ok=True, offers=[_make_offer("trial_site", 1000)]),
        ]

    notifications: list[tuple[int, str, dict]] = []

    async def fake_notify(chat_id, text, reply_markup=None):
        notifications.append((chat_id, text, reply_markup))

    sched = Scheduler(
        store=store,
        search_fn=fake_search,
        notify_fn=fake_notify,
        clock=lambda: 2000.0,
        today=lambda: date(2026, 11, 20),
        trial_sites={"trial_site"},
    )

    sent = asyncio.run(sched.run_once())
    assert sent == 0
    assert len(notifications) == 0


def test_trial_ready_thresholds(tmp_path):
    """trial_ready проверяет: >= 20 запусков, >= 90% успешных с офферами, >= 2 разных телефонов за 24 ч."""
    metrics = MetricsStore(tmp_path / "m.sqlite")
    now = 1_000_000.0

    def _put(site, ok, offers, dev, ts):
        metrics.record_run(
            source="bg",
            site=site,
            ok=bool(ok),
            partial=False,
            captcha=False,
            error=None if ok else "error",
            offers=offers,
            elapsed=3.0,
            device=dev,
            ts=ts,
        )

    # 1. Меньше 20 запусков (19 запусков, все 100% ок, 2 телефона) -> False
    for i in range(19):
        dev = "phone1" if i % 2 == 0 else "phone2"
        _put("tutu_trial", 1, 5, dev, now - 1000 + i * 10)
    assert trial_ready("tutu_trial", metrics, now=now) is False

    # Добавляем 20-й запуск -> теперь 20 запусков, 100% ок, 2 телефона -> True
    _put("tutu_trial", 1, 5, "phone1", now - 800)
    assert trial_ready("tutu_trial", metrics, now=now) is True

    # 2. Только один телефон (20 запусков, 100% ок, все на phone1) -> False
    for i in range(20):
        _put("single_phone_site", 1, 5, "phone1", now - 1000 + i * 10)
    assert trial_ready("single_phone_site", metrics, now=now) is False

    # 3. Доля успешных < 90% (20 запусков, 2 телефона, но 3 ошибки -> 17/20 = 85%) -> False
    for i in range(20):
        dev = "phone1" if i % 2 == 0 else "phone2"
        ok = 0 if i < 3 else 1
        _put("low_success_site", ok, 5 if ok else 0, dev, now - 1000 + i * 10)
    assert trial_ready("low_success_site", metrics, now=now) is False

    # 4. Успешный запуск, но 0 офферов (ok=1, offers=0 не считается успешным с офферами)
    # 20 запусков: 17 ok с офферами, 3 ok без офферов -> доля успешных 17/20 = 85% < 90% -> False
    for i in range(20):
        dev = "phone1" if i % 2 == 0 else "phone2"
        offers = 0 if i < 3 else 5
        _put("empty_offers_site", 1, offers, dev, now - 1000 + i * 10)
    assert trial_ready("empty_offers_site", metrics, now=now) is False

    # 5. Ровно 90% успешных с офферами (18 ok с офферами, 2 ошибки из 20) -> True
    for i in range(20):
        dev = "phone1" if i % 2 == 0 else "phone2"
        ok = 0 if i < 2 else 1
        _put("borderline_site", ok, 5 if ok else 0, dev, now - 1000 + i * 10)
    st = trial_stats("borderline_site", metrics, now=now)
    assert st["runs"] == 20
    assert st["ok_share"] == 0.9
    assert len(st["phones"]) == 2
    assert trial_ready("borderline_site", metrics, now=now) is True

    # 6. Запуски старше 24 часов игнорируются
    for i in range(20):
        dev = "phone1" if i % 2 == 0 else "phone2"
        _put("old_runs_site", 1, 5, dev, now - 90000 + i * 10)
    assert trial_ready("old_runs_site", metrics, now=now) is False


def test_trial_stage_flag_zero_disables():
    """TRIAL_STAGE=0 отключает разделение: все сайты считаются live."""
    live_site = SiteConfig(name="live_site", url_template="http://example.com/live", stage="live")
    trial_site = SiteConfig(name="trial_site", url_template="http://example.com/trial", stage="trial")
    settings = Settings(sites=[live_site, trial_site], trial_stage=False)

    queried_sites: list[str] = []

    async def fake_run_site(site, *args, **kwargs):
        queried_sites.append(site.name)
        return SiteResult(site=site.name, ok=True, offers=[_make_offer(site.name, 1000)])

    with patch("avia.pipeline._run_site", side_effect=fake_run_site):
        # При trial_stage=False trial_site запрашивается в живом поиске
        results = asyncio.run(search(_make_dummy_query(), settings, priority=0))

    assert set(queried_sites) == {"live_site", "trial_site"}
    assert {r.site for r in results} == {"live_site", "trial_site"}


def test_admin_panel_trial_marker():
    """В админ-панели «Парсинг» у trial-сайта к названию добавляется пометка «(пробный)»."""
    trial_site = SiteConfig(name="new_agg", url_template="http://example.com", stage="trial")
    live_site = SiteConfig(name="old_agg", url_template="http://example.com", stage="live")
    settings = Settings(sites=[trial_site, live_site], trial_stage=True)

    ctx = {
        "summary": {
            "_total": {"runs": 40, "ok": 38, "partial": 1, "captcha": 0, "failed": 1, "avg_elapsed": 2.0, "avg_offers": 10.0},
            "new_agg": {"runs": 20, "ok": 19, "partial": 0, "captcha": 0, "failed": 1, "avg_elapsed": 2.1, "avg_offers": 12.0},
            "old_agg": {"runs": 20, "ok": 19, "partial": 1, "captcha": 0, "failed": 0, "avg_elapsed": 1.9, "avg_offers": 8.0},
        },
        "settings": settings,
    }
    txt = admin_panel.build_parse(ctx, "24h")
    assert "new_agg (пробный):" in txt
    assert "old_agg (пробный):" not in txt
    assert "old_agg:" in txt

    # При отключённом trial_stage пометка не добавляется
    settings_off = Settings(sites=[trial_site, live_site], trial_stage=False)
    ctx_off = dict(ctx, settings=settings_off)
    txt_off = admin_panel.build_parse(ctx_off, "24h")
    assert "new_agg (пробный):" not in txt_off
    assert "new_agg:" in txt_off


def test_bot_maintenance_trial_promotion_notification(tmp_path):
    """bot._maintenance_loop раз в сутки проверяет trial-сайты и отправляет уведомление админам."""
    storage = tmp_path / "state"
    storage.mkdir(parents=True, exist_ok=True)
    trial_site = SiteConfig(name="trial_site", url_template="http://example.com", stage="trial")
    settings = Settings(
        sites=[trial_site],
        storage_dir=storage,
        tg_token="fake_token",
        tg_allowed_ids={12345},
        trial_stage=True,
    )
    bot = Bot(settings)
    bot.metrics = MetricsStore(storage / "metrics.sqlite")

    # Наполняем метрики: 20 успешных запусков на 2 телефонах
    now = time_now = 1_000_000.0
    for i in range(20):
        dev = "phone1" if i % 2 == 0 else "phone2"
        bot.metrics.record_run(
            source="bg",
            site="trial_site",
            ok=True,
            partial=False,
            captcha=False,
            error=None,
            offers=5,
            elapsed=2.0,
            device=dev,
            ts=now - 500 + i * 10,
        )

    sent_messages: list[str] = []

    async def fake_send(chat_id, text, **kwargs):
        sent_messages.append(text)
        return {"ok": True}

    bot.send = fake_send  # type: ignore[assignment]

    # Запускаем один шаг проверки
    with patch("time.time", return_value=now):
        # Эмулируем вызов проверки из maintenance loop
        assert trial_ready("trial_site", bot.metrics, now=now) is True
        st = trial_stats("trial_site", bot.metrics, now=now)
        runs = st["runs"]
        ok_pct = round(st["ok_share"] * 100)
        expected_msg = f"сайт trial_site готов к переводу в live ({runs} запусков, ok {ok_pct}%)"
        assert expected_msg == "сайт trial_site готов к переводу в live (20 запусков, ok 100%)"

        # Запускаем maintenance_loop на 1 тик с отменой
        async def _run_loop_briefly():
            task = asyncio.create_task(bot._maintenance_loop(tick_seconds=0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run_loop_briefly())

    assert any("сайт trial_site готов к переводу в live (20 запусков, ok 100%)" in m for m in sent_messages)
