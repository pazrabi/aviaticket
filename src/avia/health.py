"""Здоровье сайтов по телефонам: что сломалось, почему и что делать."""
from __future__ import annotations

import time
from collections import Counter
from html import escape
from typing import Any

from .metrics import MetricsStore

OK, CAPTCHA, EMPTY, NET, FAIL = "ok", "captcha", "empty", "net", "fail"

ICONS = {OK: "✅", CAPTCHA: "🧩", EMPTY: "📭", NET: "📵", FAIL: "❌"}
CAUSE_TITLES = {
    CAPTCHA: "капча",
    EMPTY: "пустая выдача",
    NET: "телефон или сеть",
    FAIL: "ошибка",
}
ADVICE = {
    CAPTCHA: "пройдите капчу вручную: откройте сайт на телефоне в браузере",
    EMPTY: "страница открывается, но билетов нет: чаще всего нужен вход в аккаунт или сменилась вёрстка; откройте сайт на телефоне и проверьте",
    NET: "телефон не отвечает или нет сети: проверьте экран, питание, интернет",
    FAIL: "смотрите «Последние ошибки» в разделе Парсинг",
}

ALERT_WINDOW_SECONDS = 3600.0
MIN_RUNS_FOR_ALERT = 3
MIN_PHONES_FOR_SITE_ALERT = 2
PAIR_FAILS_FOR_ALERT = 3
STATE_PREFIX = "health:"


def classify_run(run: dict[str, Any]) -> str:
    """Итог одного запуска: ok / captcha / empty / net / fail."""
    if run.get("ok"):
        return OK
    if run.get("captcha"):
        return CAPTCHA
    err = str(run.get("error") or "").lower()
    if any(w in err for w in ("502", "429", "таймаут", "timeout", "недоступен", "сетев", "не отвечает")):
        return NET
    if run.get("partial") or "без офферов" in err or "пуст" in err or int(run.get("offers") or 0) == 0:
        return EMPTY
    return FAIL


def _main_cause(kinds: list[str]) -> str:
    bad = [k for k in kinds if k != OK]
    return Counter(bad).most_common(1)[0][0] if bad else FAIL


def collect_health(metrics: MetricsStore, since_ts: float, per_pair: int = 5, only_sites: set[str] | None = None) -> dict[str, dict[str, list[str]]]:
    """{сайт: {телефон: [итоги последних запусков, новые первыми]}}."""
    pairs = metrics.recent_by_pair(since_ts, per_pair=per_pair)  # type: ignore[attr-defined]
    res: dict[str, dict[str, list[str]]] = {}
    for (site, dev), runs in pairs.items():
        if only_sites is not None and site not in only_sites:
            continue  # сайт убран из настроек: старые запуски не показываем
        res.setdefault(site, {})[dev] = [classify_run(r) for r in runs]
    return res


def build_health_text(health: dict[str, dict[str, list[str]]], enabled_sites: list[str] | None = None) -> str:
    """Страница «Здоровье»: по сайту — строка со статусом и ряд значков по телефонам."""
    lines = ["🩺 <b>Здоровье сайтов</b>", "Последние 5 запусков на каждом телефоне, новые слева."]
    lines.append("✅ ок · 🧩 капча · 📭 пусто · 📵 телефон/сеть · ❌ ошибка")
    sites = sorted(set(health) | set(enabled_sites or []))
    problems: list[str] = []
    for site in sites:
        phones = health.get(site, {})
        if not phones:
            lines += ["", f"<b>{escape(site)}</b>: данных нет"]
            continue
        latest_ok = [d for d, k in phones.items() if k and k[0] == OK]
        all_bad = [d for d, k in phones.items() if k and k[0] != OK]
        if not all_bad:
            head = "✅"
        elif not latest_ok:
            head = "🔴"
        else:
            head = "🟡"
        lines += ["", f"{head} <b>{escape(site)}</b>"]
        for dev in sorted(phones):
            kinds = phones[dev]
            lines.append(f"{escape(dev)}: {''.join(ICONS[k] for k in kinds)}")
        if head == "🔴":
            cause = _main_cause([k for kk in phones.values() for k in kk])
            problems.append(f"• {escape(site)}: на всех телефонах — {CAUSE_TITLES[cause]}. {ADVICE[cause]}")
    if problems:
        lines += ["", "<b>Что делать:</b>", *problems]
    return "\n".join(lines)


def check_health_alerts(metrics: MetricsStore, now: float | None = None, cooldown_hours: float = 6.0, only_sites: set[str] | None = None) -> list[str]:
    """Быстрые оповещения: сайт не работает на всех телефонах (за последний час) и пара подряд падает."""
    now_ts = time.time() if now is None else float(now)
    cooldown = cooldown_hours * 3600.0
    pairs = metrics.recent_by_pair(now_ts - ALERT_WINDOW_SECONDS, per_pair=10)  # type: ignore[attr-defined]
    by_site: dict[str, dict[str, list[str]]] = {}
    for (site, dev), runs in pairs.items():
        if only_sites is not None and site not in only_sites:
            continue
        by_site.setdefault(site, {})[dev] = [classify_run(r) for r in runs]

    messages: list[str] = []

    def _alert(key: str, bad: bool, text_bad: str, text_ok: str) -> None:
        st = metrics.get_watch_state(STATE_PREFIX + key)
        last_ts = st["last_alert_ts"] if st else 0.0
        last_status = st["last_alert_status"] if st else OK
        if bad:
            if last_status == "bad" and now_ts - last_ts < cooldown:
                return
            messages.append(text_bad)
            metrics.set_watch_state(STATE_PREFIX + key, "bad", now_ts, "bad")
        elif last_status == "bad":
            messages.append(text_ok)
            metrics.set_watch_state(STATE_PREFIX + key, OK, now_ts, OK)

    for site, phones in sorted(by_site.items()):
        total = sum(len(k) for k in phones.values())
        all_kinds = [k for kk in phones.values() for k in kk]
        site_down = (
            total >= MIN_RUNS_FOR_ALERT
            and len(phones) >= MIN_PHONES_FOR_SITE_ALERT
            and all(k != OK for k in all_kinds)
        )
        cause = _main_cause(all_kinds)
        _alert(
            f"site:{site}",
            site_down,
            f"🔴 <b>{escape(site)}</b> не работает на всех телефонах за последний час "
            f"({total} запусков, причина: {CAUSE_TITLES[cause]}).\n{ADVICE[cause]}.\n"
            f"Когда исправите, нажмите 🔄 у телефона в разделе «Телефоны».",
            f"✅ <b>{escape(site)}</b> снова работает",
        )
        if site_down:
            continue  # по телефонам отдельно не дублируем
        for dev, kinds in sorted(phones.items()):
            pair_bad = len(kinds) >= PAIR_FAILS_FOR_ALERT and all(k != OK for k in kinds[:PAIR_FAILS_FOR_ALERT])
            pc = _main_cause(kinds[:PAIR_FAILS_FOR_ALERT])
            _alert(
                f"pair:{site}:{dev}",
                pair_bad,
                f"🟡 <b>{escape(site)}</b> на телефоне <b>{escape(dev)}</b>: {PAIR_FAILS_FOR_ALERT} запуска подряд "
                f"неудачно ({CAUSE_TITLES[pc]}).\n{ADVICE[pc]}.",
                f"✅ <b>{escape(site)}</b> на телефоне <b>{escape(dev)}</b> снова работает",
            )
    return messages
