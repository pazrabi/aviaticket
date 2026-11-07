"""Достаточно ли замеров для графика истории цены."""
from avia.bot import _history_is_enough


def test_not_enough_points():
    assert _history_is_enough([]) is False
    assert _history_is_enough(None) is False
    assert _history_is_enough([(0.0, 100), (86400.0, 110), (172800.0, 120)]) is False


def test_not_enough_time_span():
    # 5 точек, но всё в пределах часа: график бессмыслен
    series = [(float(i * 600), 100 + i) for i in range(5)]
    assert _history_is_enough(series) is False


def test_enough():
    series = [(float(i * 6 * 3600), 100 + i) for i in range(5)]
    assert _history_is_enough(series) is True
