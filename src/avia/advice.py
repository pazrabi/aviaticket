"""Совет «брать или ждать» на основе правил, трендов и кривой цен."""
from __future__ import annotations

from dataclasses import dataclass
import html

# Корзины дней до вылета для кривой цен: (название, мин_дней, макс_дней)
CURVE_BUCKETS: list[tuple[str, int, int | None]] = [
    ("0-7", 0, 7),
    ("8-14", 8, 14),
    ("15-21", 15, 21),
    ("22-30", 22, 30),
    ("31-60", 31, 60),
    ("61+", 61, None),
]

VERDICT_TITLES: dict[str, str] = {
    "buy": "💡 <b>Совет: брать</b>",
    "wait": "💡 <b>Совет: подождать</b>",
    "neutral": "💡 <b>Совет: решайте сами</b>",
}

DISCLAIMER_HTML: str = "<i>Это подсказка по прошлым ценам, а не обещание: цены могут вести себя иначе.</i>"


@dataclass
class Advice:
    verdict: str
    text: str
    confidence: str
    model_prob: float | None = None


def confidence_level(samples: int) -> str:
    """Уровень уверенности по количеству замеров: 10–29 'низкая', 30–99 'средняя', 100+ 'высокая'."""
    if samples < 30:
        return "низкая"
    elif samples < 100:
        return "средняя"
    else:
        return "высокая"


def _bucket_for_days(d: int) -> str | None:
    """Определяет корзину по числу дней до вылета."""
    if d < 0:
        return None
    for name, low, high in CURVE_BUCKETS:
        if high is None:
            if d >= low:
                return name
        else:
            if low <= d <= high:
                return name
    return None


def build_advice(
    stats: dict | None,
    days_to_departure: int | None,
    curve: dict[str, dict] | None = None,
) -> Advice | None:
    """Формирует совет 'брать или ждать' на основе правил."""
    if stats is None:
        return None
    samples = stats.get("samples")
    if samples is None or samples < 10:
        return None
    p = stats.get("percentile_of_last")
    if p is None:
        return None
    if days_to_departure is None or days_to_departure < 0:
        return None
    d = days_to_departure

    # Базовые правила вердикта и текста
    if d <= 14:
        verdict = "buy"
        if p >= 40:
            text = f"Лучше брать: до вылета {d} дн., ближе к вылету цены обычно растут"
        else:
            text = f"Цена выше обычной, но до вылета {d} дн.: ждать рискованно, цены обычно растут"
    elif 15 <= d <= 45:
        if p >= 80:
            verdict = "buy"
            text = f"Цена в нижних 20% за всё время наблюдений, до вылета {d} дн. — лучше брать"
        elif p <= 30:
            verdict = "wait"
            text = f"Цена выше обычной, до вылета {d} дн. — можно подождать и последить"
        else:
            verdict = "neutral"
            text = f"Цена обычная, до вылета {d} дн. — можно брать или подождать; включите отслеживание"
    else:  # d > 45
        if p >= 80:
            verdict = "buy"
            text = f"Цена в нижних 20%, хотя вылет не скоро ({d} дн.) — хорошая возможность"
        else:
            verdict = "wait"
            text = f"До вылета {d} дн.: торопиться незачем, можно подождать"

    # Корректировка по тренду за 7 дней
    trend_7d = stats.get("trend_7d")
    trend_clause: str | None = None
    if trend_7d == "up" and d <= 30 and verdict == "wait":
        verdict = "neutral"
        trend_clause = "цены за неделю растут"
    elif trend_7d == "down" and verdict == "buy" and d > 14:
        trend_clause = "цены за неделю падают, можно ещё подождать"

    if trend_clause:
        text = f"{text}; {trend_clause}"

    # Дополнение по кривой цен (если есть данные)
    if curve:
        cur_bucket = _bucket_for_days(d)
        if cur_bucket and cur_bucket in curve and curve[cur_bucket].get("ratio") is not None:
            cur_ratio = float(curve[cur_bucket]["ratio"])
            cur_idx = [b[0] for b in CURVE_BUCKETS].index(cur_bucket)

            # Ищем ближайшую корзину ближе к вылету (с меньшим числом дней)
            closer_ratio: float | None = None
            for prev_idx in range(cur_idx - 1, -1, -1):
                prev_name = CURVE_BUCKETS[prev_idx][0]
                if prev_name in curve and curve[prev_name].get("ratio") is not None:
                    closer_ratio = float(curve[prev_name]["ratio"])
                    break

            if closer_ratio is not None and cur_ratio > 0:
                rel = closer_ratio / cur_ratio
                curve_clause: str | None = None
                if rel >= 1.05:
                    n = int(round((rel - 1.0) * 100))
                    curve_clause = f"по данным бота, ближе к вылету цены в среднем выше на {n}%"
                elif rel <= 0.95:
                    n = int(round((1.0 - rel) * 100))
                    curve_clause = f"по данным бота, ближе к вылету цены в среднем ниже на {n}%"

                if curve_clause:
                    text = f"{text}; {curve_clause}"

    confidence = confidence_level(samples)
    return Advice(verdict=verdict, text=text, confidence=confidence)


def format_advice(advice: Advice | None) -> list[str]:
    """Форматирует совет в строки HTML для Telegram."""
    if advice is None:
        return []
    header = VERDICT_TITLES.get(advice.verdict, f"💡 <b>Совет: {html.escape(advice.verdict)}</b>")
    body = f"{html.escape(advice.text.rstrip('.'))}. Уверенность: {html.escape(advice.confidence)}."
    lines = [header, body]
    if getattr(advice, "model_prob", None) is not None:
        p = advice.model_prob
        pct = int(round(p * 100)) if p <= 1.0 else int(round(p))
        lines.append(f"Модель: вероятность, что подешевеет, {pct}%")
    lines.append(DISCLAIMER_HTML)
    return lines
