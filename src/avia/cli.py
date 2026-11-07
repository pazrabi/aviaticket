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

    if len(sys.argv) > 1 and sys.argv[1] == "parser-check":
        from .samples import run_parser_check_cli
        target_dir = sys.argv[2] if len(sys.argv) > 2 else None
        code = run_parser_check_cli(target_dir)
        sys.exit(code)

    if len(sys.argv) > 1 and sys.argv[1] == "stop-calibrate":
        from .metrics import MetricsStore
        from .stop_calibrate import calibrate_all, write_rules
        s = load_settings()
        metrics = MetricsStore(s.storage_dir / "metrics.sqlite")
        dry_run = "--dry-run" in sys.argv
        results = calibrate_all(metrics, days=7)
        if not results:
            print("Нет данных для калибровки (недостаточно выборок за 7 дней)")
            return
        print(f"{'Сайт':<16} {'N (сек)':<10} {'Выборок':<10} {'Доля промахов':<15}")
        print("-" * 55)
        for site, info in sorted(results.items()):
            n_val = info.get("stable_seconds", 0.0)
            n_str = f"{n_val:.1f}" if isinstance(n_val, (int, float)) else str(n_val)
            s_val = info.get("samples", 0)
            m_val = info.get("miss_share", 0.0)
            m_str = f"{m_val * 100.0:.1f}%" if isinstance(m_val, (int, float)) else str(m_val)
            print(f"{site:<16} {n_str:<10} {s_val:<10} {m_str:<15}")
        if not dry_run:
            out_path = s.storage_dir / "stop_rules.json"
            write_rules(out_path, results)
            print(f"\nСохранено в {out_path}")
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
