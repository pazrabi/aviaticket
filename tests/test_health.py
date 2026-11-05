"""Здоровье сайтов: классификация запусков, страница и оповещения."""
from avia.health import (
    CAPTCHA, EMPTY, FAIL, NET, OK,
    build_health_text, check_health_alerts, classify_run,
)
from avia.metrics import MetricsStore


def test_classify_run():
    assert classify_run({"ok": 1}) == OK
    assert classify_run({"ok": 0, "captcha": 1}) == CAPTCHA
    assert classify_run({"ok": 0, "error": "502 Bad Gateway"}) == NET
    assert classify_run({"ok": 0, "error": "неполная выдача без офферов", "partial": 1}) == EMPTY
    assert classify_run({"ok": 0, "error": "странное", "offers": 5}) == FAIL


def _put(m, site, dev, ok, ts, captcha=0, error="", partial=0, offers=0):
    m.record_run(source="user", site=site, ok=bool(ok), partial=bool(partial), captcha=bool(captcha),
                 error=error or None, offers=offers if ok else offers, elapsed=5.0, device=dev, ts=ts)


def test_site_down_alert_then_recovery(tmp_path):
    m = MetricsStore(tmp_path / "m.sqlite")
    now = 1_000_000.0
    for i, dev in enumerate(["a", "b", "a", "b"]):
        _put(m, "tutu", dev, 0, now - 600 + i * 10, error="неполная выдача без офферов", partial=1)
    _put(m, "kupibilet", "a", 1, now - 100, offers=10)
    msgs = check_health_alerts(m, now=now)
    assert len(msgs) == 1 and "tutu" in msgs[0] and "пустая выдача" in msgs[0] and "аккаунт" in msgs[0]
    # повторно в кулдаун — молчим
    assert check_health_alerts(m, now=now + 60) == []
    # сайт ожил
    _put(m, "tutu", "a", 1, now + 100, offers=10)
    msgs = check_health_alerts(m, now=now + 120)
    assert any("tutu" in x and "снова работает" in x for x in msgs)


def test_single_pair_alert_when_others_ok(tmp_path):
    m = MetricsStore(tmp_path / "m.sqlite")
    now = 2_000_000.0
    _put(m, "yandex_travel", "a", 1, now - 50, offers=5)
    for i in range(3):
        _put(m, "yandex_travel", "b", 0, now - 300 + i * 10, captcha=1)
    msgs = check_health_alerts(m, now=now)
    assert len(msgs) == 1 and "телефоне <b>b</b>" in msgs[0] and "капча" in msgs[0]


def test_health_page_marks_red_and_advises():
    h = {"tutu": {"a": ["empty", "empty"], "b": ["empty"]}, "ozon": {"a": ["ok", "ok"]}}
    t = build_health_text(h, ["tutu", "ozon", "onetwotrip"])
    assert "🔴 <b>tutu</b>" in t and "✅ <b>ozon</b>" in t and "<b>onetwotrip</b>: данных нет" in t
    assert "Что делать" in t
