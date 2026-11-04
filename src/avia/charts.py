"""Отрисовка графиков цен авиабилетов с помощью matplotlib."""
from __future__ import annotations

from datetime import datetime
import io


def render_price_chart(
    series: list[tuple[float, int]],
    median: float | None,
    title: str,
    now_price: int | None = None,
) -> bytes | None:
    """Рендерит график динамики цен маршрута в PNG bytes.

    Возвращает None, если matplotlib не установлен или точек меньше 2.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError:
        return None

    if not series or len(series) < 2:
        return None

    sorted_series = sorted(series, key=lambda p: p[0])
    times = [datetime.fromtimestamp(ts) for ts, _ in sorted_series]
    prices = [p for _, p in sorted_series]

    fig, ax = plt.subplots(figsize=(9, 5), dpi=100)
    try:
        # Аккуратный светлый стиль, белый фон
        fig.patch.set_facecolor("#ffffff")
        ax.set_facecolor("#ffffff")

        # Линия цен
        ax.plot(
            times,
            prices,
            color="#1a73e8",
            linewidth=2.0,
            marker="o",
            markersize=4,
            label="Цена",
        )

        # Пунктирная линия медианы с подписью «обычно»
        if median is not None:
            med_val = float(median)
            ax.axhline(
                med_val,
                color="#f2994a",
                linestyle="--",
                linewidth=1.5,
                label="обычно",
            )
            ax.text(
                times[0],
                med_val,
                " обычно",
                va="bottom",
                ha="left",
                fontsize=9,
                color="#f2994a",
            )

        # Выделенная точка «сейчас» на последнем значении
        last_dt = times[-1]
        last_val = now_price if now_price is not None else prices[-1]
        ax.scatter([last_dt], [last_val], color="#e53935", s=70, zorder=5, label="сейчас")
        ax.annotate(
            "сейчас",
            (last_dt, last_val),
            textcoords="offset points",
            xytext=(0, 8),
            ha="center",
            fontsize=9,
            fontweight="bold",
            color="#e53935",
        )

        # Подписи оси дат (ДД.ММ)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        fig.autofmt_xdate(rotation=0, ha="center")

        # Цены с пробелом-разделителем тысяч и знаком ₽
        def _price_formatter(x: float, pos: object) -> str:
            return f"{int(round(x)):,}".replace(",", " ") + " ₽"

        ax.yaxis.set_major_formatter(ticker.FuncFormatter(_price_formatter))

        # Оформление: аккуратный стиль без толстых рамок
        ax.set_title(title, fontsize=13, pad=12, fontweight="bold")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#cccccc")
        ax.spines["bottom"].set_color("#cccccc")
        ax.yaxis.grid(True, linestyle="--", alpha=0.4, color="#cccccc")
        ax.xaxis.grid(False)

        ax.legend(frameon=False, loc="best")

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        return buf.getvalue()
    finally:
        plt.close(fig)
