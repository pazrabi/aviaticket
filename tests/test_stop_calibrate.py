"""Тесты калибровки stable_seconds правил остановки (src/avia/stop_calibrate.py)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from avia.admin_panel import build_parse
from avia.cli import main as cli_main
from avia.metrics import MetricsStore
from avia.stop_calibrate import (
    calibrate_all,
    calibrate_site,
    format_report,
    write_rules,
)
from avia.stop_rule import clear_cache, stable_seconds_for


@pytest.fixture(autouse=True)
def _clean_rule_cache():
    clear_cache()
    yield
    clear_cache()


def test_calibrate_site_stable_synthetic():
    """Стабильные хронологии: улучшения только в первые секунды -> выбирается минимальный N (3.0)."""
    timelines = []
    for _ in range(25):
        timelines.append({
            "points": [
                {"t": 1.0, "min": 10000, "n": 1},
                {"t": 2.0, "min": 8000, "n": 2},
            ],
            "final_min": 8000,
        })

    res = calibrate_site(timelines, min_samples=20)
    assert res is not None
    assert res["stable_seconds"] == 3.0
    assert res["samples"] == 25
    assert res["miss_share"] == 0.0


def test_calibrate_site_late_improvement():
    """Хронологии с поздним улучшением через 6 секунд после первой точки -> N подбирается = 6.0."""
    timelines = []
    # Первое улучшение на t=1.0 (min=10000), второе на t=7.0 (min=5000)
    # Дельта t = 6.0 с.
    # При N=3,4,5: дельта > N -> остановка на 10000, переплата (10000-5000)/5000 = 100% > 0.5% (промах).
    # При N=6: дельта <= 6 -> остановка на 5000 (0% промах).
    for _ in range(25):
        timelines.append({
            "points": [
                {"t": 1.0, "min": 10000, "n": 1},
                {"t": 7.0, "min": 5000, "n": 2},
            ],
            "final_min": 5000,
        })

    res = calibrate_site(timelines, min_samples=20)
    assert res is not None
    assert res["stable_seconds"] == 6.0
    assert res["samples"] == 25
    assert res["miss_share"] == 0.0


def test_calibrate_site_single_point_no_miss():
    """Хронологии без улучшений после первой точки считаются без промаха."""
    timelines = []
    for _ in range(25):
        timelines.append({
            "points": [
                {"t": 1.5, "min": 7500, "n": 1},
            ],
            "final_min": 7500,
        })

    res = calibrate_site(timelines, min_samples=20)
    assert res is not None
    assert res["stable_seconds"] == 3.0
    assert res["miss_share"] == 0.0


def test_calibrate_site_fallback_to_grid_max():
    """Если все N дают долю промахов > max_miss_share, берётся максимум сетки."""
    timelines = []
    # Позднее улучшение через 25 секунд (вне сетки)
    for _ in range(25):
        timelines.append({
            "points": [
                {"t": 1.0, "min": 10000, "n": 1},
                {"t": 26.0, "min": 4000, "n": 2},
            ],
            "final_min": 4000,
        })

    res = calibrate_site(timelines, grid=(3, 4, 5, 8, 10, 15), min_samples=20)
    assert res is not None
    assert res["stable_seconds"] == 15.0
    assert res["miss_share"] == 1.0


def test_calibrate_site_not_enough_samples():
    """Мало выборок (< min_samples) -> возвращает None."""
    few_timelines = [
        {"points": [{"t": 1.0, "min": 5000}], "final_min": 5000}
        for _ in range(10)
    ]
    assert calibrate_site(few_timelines, min_samples=20) is None
    assert calibrate_site([], min_samples=20) is None


def test_calibrate_site_filters_invalid_timelines():
    """Игнорируются хронологии с пустыми points или отсутствующим final_min."""
    timelines = [
        {"points": [], "final_min": 5000},
        {"points": [{"t": 1.0, "min": 5000}], "final_min": None},
        {"points": None, "final_min": 5000},
    ]
    assert calibrate_site(timelines, min_samples=1) is None


def test_write_rules_atomic(tmp_path: Path):
    """Атомарная запись results в stop_rules.json."""
    rules_file = tmp_path / "stop_rules.json"
    results = {
        "aviasales": {"stable_seconds": 6.0, "samples": 40, "miss_share": 0.01},
        "kupibilet": {"stable_seconds": 5.0, "samples": 30, "miss_share": 0.0},
    }
    write_rules(rules_file, results)
    assert rules_file.is_file()

    data = json.loads(rules_file.read_text(encoding="utf-8"))
    assert data["aviasales"]["stable_seconds"] == 6.0
    assert data["aviasales"]["samples"] == 40
    assert data["aviasales"]["miss_share"] == 0.01
    assert "updated" in data["aviasales"]
    assert data["kupibilet"]["stable_seconds"] == 5.0


def test_write_rules_empty_does_not_overwrite(tmp_path: Path):
    """Пустой results не затирает существующий файл."""
    rules_file = tmp_path / "stop_rules.json"
    initial_content = json.dumps({"existing": {"stable_seconds": 7.0}})
    rules_file.write_text(initial_content, encoding="utf-8")

    write_rules(rules_file, {})
    assert rules_file.read_text(encoding="utf-8") == initial_content

    write_rules(rules_file, None)  # type: ignore[arg-type]
    assert rules_file.read_text(encoding="utf-8") == initial_content


def test_stable_seconds_for_reads_written_rules(tmp_path: Path):
    """чтение через stable_seconds_for даёт записанное значение."""
    rules_file = tmp_path / "stop_rules.json"
    results = {
        "aviasales": {"stable_seconds": 6.5, "samples": 25, "miss_share": 0.0},
    }
    write_rules(rules_file, results)

    settings = SimpleNamespace(storage_dir=tmp_path, stop_stable_seconds=8.0)
    clear_cache()
    assert stable_seconds_for("aviasales", settings) == 6.5
    assert stable_seconds_for("kupibilet", settings) == 8.0  # дефолтное


def test_format_report_and_admin_panel(tmp_path: Path):
    """Форматирование отчёта и отображение строки в админ-панели."""
    # 1. format_report
    assert format_report({}) == ""
    sample_rules = {
        "aviasales": {"stable_seconds": 6.0},
        "kupibilet": {"stable_seconds": 5.0},
    }
    rep = format_report(sample_rules)
    assert rep == "🎯 Время ожидания стабильной цены: aviasales 6 с · kupibilet 5 с"

    # 2. build_parse в админ-панели
    settings = SimpleNamespace(storage_dir=tmp_path, trial_stage=False, sites=[])
    ctx = {
        "settings": settings,
        "summary": {
            "_total": {"runs": 10, "ok": 10, "partial": 0, "captcha": 0, "failed": 0, "avg_elapsed": 5.0, "avg_offers": 10},
            "aviasales": {"runs": 10, "ok": 10, "partial": 0, "captcha": 0, "failed": 0, "avg_elapsed": 5.0, "avg_offers": 10},
        },
    }

    # Файла ещё нет -> строки нет в панели
    out_no_file = build_parse(ctx)
    assert "🎯 Время ожидания стабильной цены:" not in out_no_file

    # Записываем stop_rules.json
    rules_file = tmp_path / "stop_rules.json"
    write_rules(rules_file, sample_rules)

    out_with_file = build_parse(ctx)
    assert "🎯 Время ожидания стабильной цены: aviasales 6 с · kupibilet 5 с" in out_with_file

    # Битый файл не ломает панель и строка просто не появляется
    rules_file.write_text("invalid json {{{", encoding="utf-8")
    out_corrupted = build_parse(ctx)
    assert "🎯 Время ожидания стабильной цены:" not in out_corrupted


def test_calibrate_all(tmp_path: Path):
    """calibrate_all калибрует только сайты с достаточным количеством выборок."""
    store = MetricsStore(tmp_path / "metrics.sqlite")
    now = time.time()

    # site_a: 25 хронологий (хватает для min_samples=20)
    for i in range(25):
        store.record_timeline(
            site="site_a",
            points=[{"t": 1.0, "min": 5000, "n": 1}],
            final_min=5000,
            ts=now - i * 3600,
        )

    # site_b: 5 хронологий (не хватает)
    for i in range(5):
        store.record_timeline(
            site="site_b",
            points=[{"t": 1.0, "min": 4000, "n": 1}],
            final_min=4000,
            ts=now - i * 3600,
        )

    res = calibrate_all(store, days=7, now=now, min_samples=20)
    assert "site_a" in res
    assert "site_b" not in res
    assert res["site_a"]["stable_seconds"] == 3.0


def test_cli_stop_calibrate(tmp_path: Path, monkeypatch, capsys):
    """Команда cli stop-calibrate: вывод таблицы и флаг --dry-run."""
    metrics_path = tmp_path / "metrics.sqlite"
    store = MetricsStore(metrics_path)
    now = time.time()
    for i in range(25):
        store.record_timeline(
            site="site_cli",
            points=[{"t": 1.0, "min": 5000, "n": 1}],
            final_min=5000,
            ts=now - i * 3600,
        )

    fake_settings = SimpleNamespace(
        storage_dir=tmp_path,
        top_n=5,
        headless=True,
    )
    monkeypatch.setattr("avia.cli.load_settings", lambda: fake_settings)

    # 1. С флагом --dry-run: файл не создаётся
    monkeypatch.setattr(sys, "argv", ["avia.cli", "stop-calibrate", "--dry-run"])
    cli_main()
    out = capsys.readouterr().out
    assert "site_cli" in out
    assert not (tmp_path / "stop_rules.json").exists()

    # 2. Без --dry-run: файл создаётся
    monkeypatch.setattr(sys, "argv", ["avia.cli", "stop-calibrate"])
    cli_main()
    out2 = capsys.readouterr().out
    assert "site_cli" in out2
    assert (tmp_path / "stop_rules.json").exists()


def test_bot_last_stop_calibrate_and_config(tmp_path: Path, monkeypatch):
    """Бот инициализирует self._last_stop_calibrate = 0.0, настройка STOP_CALIBRATE работает."""
    from avia.bot import Bot
    from avia.config import Settings, load_settings

    # 1. Проверяем config default и отключение через STOP_CALIBRATE=0
    monkeypatch.setenv("STOP_CALIBRATE", "1")
    s1 = load_settings()
    assert s1.stop_calibrate is True

    monkeypatch.setenv("STOP_CALIBRATE", "0")
    s0 = load_settings()
    assert s0.stop_calibrate is False

    # 2. Инициализация в Bot
    bot_settings = Settings(
        sites=[],
        storage_dir=tmp_path,
        tg_token="fake",
        tg_open=True,
        stop_calibrate=True,
    )
    bot = Bot(bot_settings)
    assert hasattr(bot, "_last_stop_calibrate")
    assert bot._last_stop_calibrate == 0.0

