"""Аналитика цен авиабилетов: текстовые формулировки для вывода в Telegram."""
from __future__ import annotations

import html

SITE_NAMES: dict[str, str] = {
    "aviasales": "Aviasales",
    "kupibilet": "Купибилет",
    "yandex_travel": "Яндекс Путешествия",
    "tutu": "Туту",
    "onetwotrip": "OneTwoTrip",
    "ozon_travel": "Ozon Travel",
}

TREND_WORDS: dict[str, str] = {
    "up": "растёт",
    "down": "падает",
    "flat": "стоит",
}

TREND_ICONS: dict[str, str] = {
    "up": "📈",
    "down": "📉",
    "flat": "➡️",
}


def site_label(site: str) -> str:
    """Человекочитаемое название сайта бронирования."""
    return SITE_NAMES.get(site, site)


def position_line(stats: dict | None) -> str | None:
    """Строка положения цены: дешевле/дороже по отношению к истории маршрута."""
    if not stats:
        return None
    p = stats.get("percentile_of_last")
    if p is None:
        return None
    n = int(round(p))
    if n >= 80:
        return f"💚 Дешевле, чем в {n}% замеров по этому маршруту"
    elif n >= 40:
        return f"Цена обычная: дешевле, чем в {n}% замеров"
    else:
        m = 100 - n
        return f"🔺 Дороже, чем в {m}% замеров"


def trend_line(stats: dict | None) -> str | None:
    """Строка динамики цен: тренд за 3 и 7 дней."""
    if not stats:
        return None
    t3 = stats.get("trend_3d")
    t7 = stats.get("trend_7d")
    if t3 is None and t7 is None:
        return None

    if t3 is not None and t7 is not None and t3 == t7:
        icon = TREND_ICONS.get(t3, "")
        word = TREND_WORDS.get(t3, t3)
        return f"{icon} За 3 и 7 дней {word}".strip()

    if t3 is not None and t7 is not None:
        icon = TREND_ICONS.get(t3, "")
        w3 = TREND_WORDS.get(t3, t3)
        w7 = TREND_WORDS.get(t7, t7)
        return f"{icon} За 3 дня {w3}, за 7 дней {w7}".strip()

    if t3 is not None:
        icon = TREND_ICONS.get(t3, "")
        w3 = TREND_WORDS.get(t3, t3)
        return f"{icon} За 3 дня {w3}".strip()

    if t7 is not None:
        icon = TREND_ICONS.get(t7, "")
        w7 = TREND_WORDS.get(t7, t7)
        return f"{icon} За 7 дней {w7}".strip()

    return None


def volatility_line(stats: dict | None) -> str | None:
    """Строка стабильности цен на маршруте."""
    if not stats:
        return None
    v = stats.get("volatility_pct")
    if v is None:
        return None
    if v < 5:
        return "Цена стабильная"
    return f"Цена скачет ±{round(v)}%"


def best_site_line(site_stats: list[tuple[str, int, int]] | None) -> str | None:
    """Строка сайта, который чаще всего оказывается самым дешёвым."""
    if not site_stats:
        return None
    site, k, m = site_stats[0]
    if m < 5 or k <= 0:
        return None
    label = html.escape(site_label(site))
    return f"Чаще всего дешевле на {label} (в {k} из {m} замеров)"


def summary_lines(
    stats: dict | None,
    site_stats: list[tuple[str, int, int]] | None = None,
) -> list[str]:
    """Собирает непустые строки аналитики в порядке: position, trend, volatility, best_site."""
    lines: list[str] = []
    pos = position_line(stats)
    if pos:
        lines.append(pos)
    tr = trend_line(stats)
    if tr:
        lines.append(tr)
    vol = volatility_line(stats)
    if vol:
        lines.append(vol)
    best = best_site_line(site_stats)
    if best:
        lines.append(best)
    return lines
