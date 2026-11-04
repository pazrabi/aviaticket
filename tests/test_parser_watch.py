"""Тесты для модуля src/avia/parser_watch.py (мониторинг парсеров, пороги, кулдаун, восстановление)."""
from __future__ import annotations

import time
from html import escape
import pytest

from avia.config import Settings, load_settings
from avia.metrics import MetricsStore
from avia.parser_watch import (
    STATUS_BROKEN,
    STATUS_DEGRADED,
    STATUS_FEW_RUNS,
    STATUS_OK,
    check_parser_watch,
    evaluate_status,
    get_parser_statuses,
)
from avia.admin_panel import build_parse


def test_evaluate_status_thresholds():
    """Проверка порогов: min_runs, min_ok_pct, max_incomplete_pct."""
    # Меньше min_runs (8) -> мало данных
    assert evaluate_status(runs=7, ok=0, partial=0, min_runs=8) == STATUS_FEW_RUNS
    assert evaluate_status(runs=0, ok=0, partial=0, min_runs=8) == STATUS_FEW_RUNS

    # 10 запусков, 5 ок (50% < 60%) -> сломан
    assert evaluate_status(runs=10, ok=5, partial=0, min_runs=8, min_ok_pct=60) == STATUS_BROKEN

    # 10 запусков, 8 ок (80% >= 60%), но 7 неполных (70% > 60%) -> деградирует
    assert evaluate_status(runs=10, ok=8, partial=7, min_runs=8, min_ok_pct=60, max_incomplete_pct=60) == STATUS_DEGRADED

    # 10 запусков, 10 ок, 1 неполный -> ок
    assert evaluate_status(runs=10, ok=10, partial=1, min_runs=8, min_ok_pct=60, max_incomplete_pct=60) == STATUS_OK

    # Граничные значения
    # 60% ok ровно -> не ниже 60% -> ок
    assert evaluate_status(runs=10, ok=6, partial=0, min_runs=8, min_ok_pct=60) == STATUS_OK
    # 60% partial ровно -> не больше 60% -> ок
    assert evaluate_status(runs=10, ok=10, partial=6, min_runs=8, max_incomplete_pct=60) == STATUS_OK


def test_parser_watch_cooldown_and_no_duplicate_alert(tmp_path):
    """Повторное сообщение не уходит, пока действует кулдаун, и уходит после его истечения."""
    db_path = tmp_path / "metrics.sqlite"
    metrics = MetricsStore(db_path)
    s = Settings(
        sites=[],
        parser_watch_hours=6,
        parser_watch_min_runs=8,
        parser_watch_min_ok_pct=60,
        parser_watch_cooldown_hours=6,
    )
    t0 = 100000.0

    # Записываем 10 запусков для 'site_fail', из них только 2 ok (20%)
    for i in range(10):
        metrics.record_run("user", "site_fail", ok=(i < 2), partial=0, captcha=0, error=None, offers=1, elapsed=1.0, ts=t0 + i * 60)

    # Первый запуск проверки: должен вернуть сообщение о пробое
    msgs1 = check_parser_watch(metrics, s, now=t0 + 700)
    assert len(msgs1) == 1
    assert "site_fail" in msgs1[0]
    assert "сломан" in msgs1[0]
    assert "ок 20% за 6 ч (10 запусков)" in msgs1[0]

    # Второй запуск через 30 минут: действует кулдаун (6 часов), сообщение НЕ должно уйти
    msgs2 = check_parser_watch(metrics, s, now=t0 + 700 + 1800)
    assert len(msgs2) == 0

    # Перезапуск бота: создаём новый экземпляр MetricsStore к той же базе
    metrics_restarted = MetricsStore(db_path)
    msgs_restarted = check_parser_watch(metrics_restarted, s, now=t0 + 700 + 3600)
    assert len(msgs_restarted) == 0

    # Спустя 6.5 часов: сайт продолжает падать (новые 10 запусков в новом окне)
    t2 = t0 + 6.5 * 3600
    for i in range(10):
        metrics.record_run("user", "site_fail", ok=(i < 2), partial=0, captcha=0, error=None, offers=1, elapsed=1.0, ts=t2 + i * 60)

    # Кулдаун истёк (> 6 ч с первого алерта), повторное сообщение отправляется
    msgs3 = check_parser_watch(metrics, s, now=t2 + 700)
    assert len(msgs3) == 1
    assert "site_fail" in msgs3[0]
    assert "сломан" in msgs3[0]


def test_parser_watch_recovery_message(tmp_path):
    """Сообщение о восстановлении отправляется один раз, когда показатели вернулись в норму."""
    metrics = MetricsStore(tmp_path / "metrics.sqlite")
    s = Settings(
        sites=[],
        parser_watch_hours=6,
        parser_watch_min_runs=8,
        parser_watch_min_ok_pct=60,
        parser_watch_cooldown_hours=6,
    )
    t0 = 200000.0

    # Шаг 1: 10 неудачных запусков (все ok=0)
    for i in range(10):
        metrics.record_run("user", "site_rec", ok=0, partial=0, captcha=0, error="err", offers=0, elapsed=1.0, ts=t0 + i * 60)

    # Проверка выявляет сбой
    msgs_fail = check_parser_watch(metrics, s, now=t0 + 700)
    assert len(msgs_fail) == 1
    assert "сломан" in msgs_fail[0]

    # Шаг 2: Ситуация исправилась: следующие 10 запусков в новом окне все успешные
    t1 = t0 + 7 * 3600  # сдвиг на 7 часов (старые 10 запусков вышли из 6-часового окна)
    for i in range(10):
        metrics.record_run("user", "site_rec", ok=1, partial=0, captcha=0, error=None, offers=5, elapsed=1.0, ts=t1 + i * 60)

    # Проверка должна отправить ОДИН раз сообщение о восстановлении
    msgs_rec1 = check_parser_watch(metrics, s, now=t1 + 700)
    assert len(msgs_rec1) == 1
    assert "восстановился" in msgs_rec1[0]
    assert "site_rec" in msgs_rec1[0]
    assert "ок 100% за 6 ч (10 запусков)" in msgs_rec1[0]

    # Следующая проверка спустя 30 минут: сайт по-прежнему ок, повторного сообщения быть не должно!
    msgs_rec2 = check_parser_watch(metrics, s, now=t1 + 700 + 1800)
    assert len(msgs_rec2) == 0


def test_parser_watch_few_runs_no_alert(tmp_path):
    """Мало запусков (меньше parser_watch_min_runs) — сообщений нет."""
    metrics = MetricsStore(tmp_path / "metrics.sqlite")
    s = Settings(
        sites=[],
        parser_watch_hours=6,
        parser_watch_min_runs=8,
        parser_watch_min_ok_pct=60,
    )
    t0 = 300000.0

    # Всего 3 запуска (все упали)
    for i in range(3):
        metrics.record_run("user", "site_rare", ok=0, partial=0, captcha=0, error="err", offers=0, elapsed=1.0, ts=t0 + i * 60)

    msgs = check_parser_watch(metrics, s, now=t0 + 300)
    assert len(msgs) == 0


def test_parser_watch_degraded_alert(tmp_path):
    """Оповещение о деградации при превышении доли неполных выдач."""
    metrics = MetricsStore(tmp_path / "metrics.sqlite")
    s = Settings(
        sites=[],
        parser_watch_hours=6,
        parser_watch_min_runs=8,
        parser_watch_min_ok_pct=60,
        parser_watch_max_incomplete_pct=60,
    )
    t0 = 400000.0

    # 10 запусков: 9 ok, но 7 partial (70% > 60%)
    for i in range(10):
        metrics.record_run("user", "site_deg", ok=(i < 9), partial=(i < 7), captcha=0, error=None, offers=2, elapsed=1.0, ts=t0 + i * 60)

    msgs = check_parser_watch(metrics, s, now=t0 + 700)
    assert len(msgs) == 1
    assert "деградирует" in msgs[0]
    assert "неполных 70%" in msgs[0]


def test_admin_panel_status_line():
    """В разделе Парсинг панели /admin выводится строка-статус по сайтам («ок», «деградирует», «сломан»)."""
    ctx = {
        "summary": {
            "_total": {"runs": 30, "ok": 20, "partial": 2, "captcha": 0, "failed": 10, "avg_elapsed": 2.0, "avg_offers": 10.0},
            "siteA": {"runs": 10, "ok": 10, "partial": 0, "captcha": 0, "failed": 0, "avg_elapsed": 1.5, "avg_offers": 10.0},
            "siteB": {"runs": 10, "ok": 2, "partial": 0, "captcha": 0, "failed": 8, "avg_elapsed": 1.5, "avg_offers": 1.0},
            "siteC": {"runs": 10, "ok": 8, "partial": 7, "captcha": 0, "failed": 2, "avg_elapsed": 1.5, "avg_offers": 3.0},
        },
        "parser_statuses": {
            "siteA": "ок",
            "siteB": "сломан",
            "siteC": "деградирует",
        },
    }
    txt = build_parse(ctx, "24h")
    assert "Статус: siteA: ок · siteB: сломан · siteC: деградирует" in txt
    assert escape("siteA") in txt


def test_admin_panel_fallback_evaluate_status():
    """Если parser_statuses отсутствует в контексте, вычисляется через evaluate_status с учётом settings."""
    ctx = {
        "summary": {
            "_total": {"runs": 30, "ok": 20, "partial": 2, "captcha": 0, "failed": 10, "avg_elapsed": 2.0, "avg_offers": 10.0},
            "siteA": {"runs": 10, "ok": 10, "partial": 0, "captcha": 0, "failed": 0, "avg_elapsed": 1.5, "avg_offers": 10.0},
            "siteB": {"runs": 10, "ok": 2, "partial": 0, "captcha": 0, "failed": 8, "avg_elapsed": 1.5, "avg_offers": 1.0},
            "siteC": {"runs": 10, "ok": 8, "partial": 7, "captcha": 0, "failed": 2, "avg_elapsed": 1.5, "avg_offers": 3.0},
        },
        # parser_statuses отсутствует
    }
    txt = build_parse(ctx, "24h")
    assert "Статус: siteA: ок · siteB: сломан · siteC: деградирует" in txt

    # С кастомными settings (min_runs=15) -> для 10 запусков статус "мало данных"
    custom_s = Settings(sites=[], parser_watch_min_runs=15)
    ctx["settings"] = custom_s
    txt2 = build_parse(ctx, "24h")
    assert "Статус: siteA: мало данных · siteB: мало данных · siteC: мало данных" in txt2


def test_parser_watch_env_settings(monkeypatch):
    """Настройки загружаются из переменных окружения PARSER_WATCH_*."""
    monkeypatch.setenv("PARSER_WATCH_ENABLED", "0")
    monkeypatch.setenv("PARSER_WATCH_HOURS", "12")
    monkeypatch.setenv("PARSER_WATCH_MIN_RUNS", "15")
    monkeypatch.setenv("PARSER_WATCH_MIN_OK_PCT", "70")
    monkeypatch.setenv("PARSER_WATCH_MAX_INCOMPLETE_PCT", "50")
    monkeypatch.setenv("PARSER_WATCH_COOLDOWN_HOURS", "4")

    s = load_settings()
    assert s.parser_watch_enabled is False
    assert s.parser_watch_hours == 12
    assert s.parser_watch_min_runs == 15
    assert s.parser_watch_min_ok_pct == 70
    assert s.parser_watch_max_incomplete_pct == 50
    assert s.parser_watch_cooldown_hours == 4
