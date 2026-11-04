"""Тесты блока «Прогноз» в панели разработчика /admin."""
from __future__ import annotations

from html import escape
import pytest

from avia import admin_panel


def test_format_forecast_block_empty():
    """Пустой контекст или отсутствие отчёта: сообщение, что модель ещё не обучалась."""
    lines = admin_panel.format_forecast_block(None)
    assert lines[0] == "<b>Прогноз:</b>"
    assert "Модель ещё не обучалась" in lines[1]

    lines_dict = admin_panel.format_forecast_block({})
    assert "Модель ещё не обучалась" in lines_dict[1]


def test_format_forecast_block_early():
    """Отчёт с вердиктом «рано: нужно ещё N ответов»."""
    fc = {
        "trained_at": "2026-11-04T12:30:00",
        "train_rows": 15,
        "test_rows": 3,
        "train_routes": 4,
        "test_routes": 1,
        "departed_flights_count": 5,
        "verdict": "рано: нужно ещё 485 ответов",
        "metrics": None,
    }
    lines = admin_panel.format_forecast_block(fc)
    text = "\n".join(lines)
    assert "<b>Прогноз:</b>" in text
    assert "04.11 12:30" in text
    assert "Строк: обучение 15 · проверка 3" in text
    assert "Вылетевших рейсов с ответами: 5" in text
    assert "Вердикт: рано: нужно ещё 485 ответов" in text


def test_format_forecast_block_with_metrics_and_escaping():
    """Отчёт с метриками и проверка экранирования HTML в динамических полях."""
    fc = {
        "trained_at": "2026-11-04T15:00:00",
        "train_rows": 600,
        "test_rows": 150,
        "routes_count": 25,
        "departed_flights_count": 30,
        "verdict": "<лучше правил & быстрее>",
        "metrics": {
            "model": {
                "accuracy": 0.88,
                "precision": 0.82,
                "recall": 0.79,
                "missed_opportunity_pct": 2.4,
            },
            "baseline_always_buy": {
                "accuracy": 0.65,
                "precision": 0.0,
                "recall": 0.0,
                "missed_opportunity_pct": 7.1,
            },
            "baseline_rules": {
                "accuracy": 0.72,
                "precision": 0.60,
                "recall": 0.55,
                "missed_opportunity_pct": 5.0,
            },
        },
    }
    lines = admin_panel.format_forecast_block(fc)
    text = "\n".join(lines)
    assert "<b>Прогноз:</b>" in text
    assert "Строк: обучение 600 · проверка 150" in text
    assert "Маршрутов: 25 · Вылетевших рейсов с ответами: 30" in text
    assert "точность 88%" in text
    assert "упущенная выгода 2.4%" in text
    assert "«всегда брать» 65%" in text
    assert "правила 72%" in text
    # HTML экранирование
    assert "&lt;лучше правил &amp; быстрее&gt;" in text
    assert "<лучше правил" not in text


def test_build_tickets_includes_forecast_block():
    """Раздел «Билеты» включает блок «Прогноз», если передан ключ forecast."""
    ctx = {
        "routes_count": 5,
        "searches_count": 20,
        "avg_days_to_departure": 12.0,
        "popular_routes": [],
        "baggage": {},
        "site_share": [],
        "forecast": {
            "trained_at": "2026-11-04T12:00:00",
            "train_rows": 550,
            "test_rows": 120,
            "routes_count": 22,
            "departed_flights_count": 22,
            "verdict": "лучше правил",
        },
    }
    txt = admin_panel.build_tickets(ctx)
    assert "🎫 <b>Билеты</b>" in txt
    assert "<b>Прогноз:</b>" in txt
    assert "Вердикт: лучше правил" in txt


def test_build_forecast_section():
    """Отдельная функция build_forecast выводит раздел прогноза."""
    ctx = {
        "forecast": {
            "trained_at": "2026-11-04T10:00:00",
            "train_rows": 0,
            "test_rows": 0,
            "verdict": "рано: нужно ещё 500 ответов",
        }
    }
    txt = admin_panel.build_forecast(ctx)
    assert "🔮 <b>Прогноз</b>" in txt
    assert "<b>Прогноз:</b>" in txt
    assert "рано: нужно ещё 500 ответов" in txt


def test_build_forecast_section_uninitialized_fallback():
    """Раздел прогноза до первого обучения показывает информативный вердикт «рано» и вылетевшие рейсы."""
    ctx = {
        "forecast": {
            "trained_at": None,
            "train_rows": 0,
            "test_rows": 0,
            "routes_count": 0,
            "departed_flights_count": 3,
            "verdict": "рано: нужно ещё 500 ответов",
            "metrics": None,
        }
    }
    txt = admin_panel.build_forecast(ctx)
    assert "🔮 <b>Прогноз</b>" in txt
    assert "• Обучение: —" in txt
    assert "• Строк: обучение 0 · проверка 0" in txt
    assert "Вылетевших рейсов с ответами: 3" in txt
    assert "• Вердикт: рано: нужно ещё 500 ответов" in txt
