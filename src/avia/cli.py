"""Запуск из консоли без Telegram: python -m avia.cli Москва Алматы 10.12"""
from __future__ import annotations

import asyncio
import re
import sys

from .config import load_settings
from .formatting import format_report
from .guard import friendly_error
from .parsing import ParseError, parse_query
from .pipeline import search


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "forecast-train":
        import json
        from . import forecast
        from .history import PriceHistory
        s = load_settings()
        hist = PriceHistory(s.storage_dir / "history.sqlite")
        rep = forecast.train_and_eval(hist, s.forecast_dir)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return

    try:
        q = parse_query(" ".join(a for a in sys.argv[1:] if not a.startswith("--")))
    except ParseError as e:
        sys.exit(str(e))
    s = load_settings()
    s.headless = "--show" not in sys.argv  # --show: видеть браузер
    try:
        results = asyncio.run(search(q, s))
    except Exception as e:
        sys.exit(f"⚠️ {friendly_error(e)}")
    print(re.sub(r"<[^>]+>", "", format_report(q, results, s.top_n)))


if __name__ == "__main__":
    main()
