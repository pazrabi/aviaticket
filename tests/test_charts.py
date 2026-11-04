"""Тесты модуля отрисовки графиков цен (charts.py)."""
from datetime import datetime
import time
from unittest.mock import patch

import pytest

from avia.charts import render_price_chart

matplotlib = pytest.importorskip("matplotlib")
import matplotlib.pyplot as plt


def test_render_price_chart_returns_png():
    """Корректный график возвращает байты с сигнатурой PNG."""
    now = time.time()
    series = [
        (now - 3 * 86400, 15000),
        (now - 2 * 86400, 14200),
        (now - 1 * 86400, 13900),
        (now, 13500),
    ]
    png_bytes = render_price_chart(series, median=14000.0, title="Москва → Ереван", now_price=13500)
    assert png_bytes is not None
    assert png_bytes.startswith(b"\x89PNG")
    # Проверяем отсутствие утечки открытых фигур matplotlib
    assert plt.get_fignums() == []


def test_render_price_chart_less_than_two_points():
    """При количестве точек меньше 2 функция возвращает None."""
    now = time.time()
    assert render_price_chart([], median=10000.0, title="Пустой") is None
    assert render_price_chart([(now, 10000)], median=10000.0, title="Одна точка") is None


def test_render_price_chart_without_median_and_now_price():
    """График успешно строится без медианы и без явного now_price (берётся последняя точка)."""
    now = time.time()
    series = [
        (now - 86400, 12000),
        (now, 11500),
    ]
    png_bytes = render_price_chart(series, median=None, title="Без медианы")
    assert png_bytes is not None
    assert png_bytes.startswith(b"\x89PNG")
    assert plt.get_fignums() == []


def test_render_price_chart_handles_missing_matplotlib():
    """Если matplotlib недоступен, функция возвращает None."""
    now = time.time()
    series = [(now - 86400, 10000), (now, 11000)]

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ImportError("No module named 'matplotlib'")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        res = render_price_chart(series, median=10500.0, title="Тест")
        assert res is None
