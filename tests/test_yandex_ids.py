"""Тесты для src/avia/yandex_ids.py и интеграции с sites.yaml и pipeline.py."""
from __future__ import annotations

from datetime import date
import pytest

from avia.collector import Snapshot
from avia.config import load_settings
from avia.models import Query
from avia.sites import build_url
from avia.yandex_ids import STATIC_CITIES, city_id, clear_cache


@pytest.fixture(autouse=True)
def reset_yandex_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "test_yandex_ids_cache.json"
    monkeypatch.setenv("YANDEX_IDS_CACHE", str(cache_path))
    clear_cache()
    yield
    clear_cache()


def test_city_id_known_codes():
    """city_id возвращает верные идентификаторы для всех известных IATA-кодов."""
    expected_all = {
        "MOW": "c213",
        "LED": "c2",
        "KUF": "c51",
        "SVX": "c54",
        "KZN": "c43",
        "AER": "c239",
        "OVB": "c65",
        "UFA": "c172",
        "MSQ": "c157",
        "ALA": "c22177",
        "NQZ": "c163",
        "TAS": "c10335",
        "SKD": "c10334",
        "FRU": "c10309",
        "EVN": "c10262",
        "GYD": "c10253",
        "TBS": "c10277",
        "DYU": "c10318",
        "IST": "c11508",
        "DXB": "c11499",
    }
    assert STATIC_CITIES == expected_all

    for code, expected_id in expected_all.items():
        assert city_id(code) == expected_id


def test_city_id_case_insensitive_and_whitespace():
    """city_id игнорирует регистр символов и пробелы по краям."""
    assert city_id("mow") == "c213"
    assert city_id("Mow") == "c213"
    assert city_id("  ala  ") == "c22177"
    assert city_id("Ist") == "c11508"
    assert city_id("dxb") == "c11499"


def test_city_id_unknown_code_raises_runtime_error():
    """Неизвестный IATA-код вызывает RuntimeError с точным текстом ошибки."""
    with pytest.raises(
        RuntimeError,
        match=r"^Яндекс Путешествия: город XYZ пока не поддерживается \(нет id в таблице yandex_ids\.py\)$",
    ):
        city_id("XYZ")

    with pytest.raises(
        RuntimeError,
        match=r"^Яндекс Путешествия: город bbb пока не поддерживается \(нет id в таблице yandex_ids\.py\)$",
    ):
        city_id("bbb")


def test_build_url_from_sites_yaml_template():
    """build_url с extra даёт точный URL для Query('MOW','ALA',date(2026,12,12)) по шаблону из sites.yaml."""
    settings = load_settings()
    site = next(s for s in settings.sites if s.name == "yandex_travel")

    assert site.enabled is True
    assert site.id_resolver == "yandex"
    assert site.parser == "yandex_travel"
    assert site.settle_ms == 30000
    assert site.scrolls == 1

    q = Query("MOW", "ALA", date(2026, 12, 12))
    extra = {
        "origin_id": city_id(q.origin),
        "destination_id": city_id(q.destination),
    }
    url = build_url(site, q, extra=extra)
    expected_url = (
        "https://travel.yandex.ru/avia/search/result/"
        "?adult_seats=1&children_seats=0&fromId=c213&infant_seats=0&klass=economy&oneway=1&salesChannel=B2C&toId=c22177&when=2026-12-12"
    )
    assert url == expected_url


@pytest.mark.asyncio
async def test_pipeline_yandex_id_resolution(monkeypatch):
    """В pipeline._from_browser при id_resolver == 'yandex' extra-параметры подставляются в build_url."""
    from avia.models import Offer
    from avia.parsers import PARSERS
    from avia.pipeline import _from_browser

    captured_urls: list[str] = []

    async def fake_fetch_snapshot(url, site, **kwargs):
        captured_urls.append(url)
        return Snapshot(url=url, text="OK", json_payloads=[])

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)
    monkeypatch.setitem(PARSERS, "yandex_travel", lambda snap, q: [Offer(site=snap.url, price=12345)])

    settings = load_settings()
    site = next(s for s in settings.sites if s.name == "yandex_travel")
    q = Query("MOW", "ALA", date(2026, 12, 12))

    offers, page_url, partial = await _from_browser(site, q, settings)
    assert len(captured_urls) == 1
    assert captured_urls[0] == (
        "https://travel.yandex.ru/avia/search/result/"
        "?adult_seats=1&children_seats=0&fromId=c213&infant_seats=0&klass=economy&oneway=1&salesChannel=B2C&toId=c22177&when=2026-12-12"
    )
    assert len(offers) == 1
    assert offers[0].price == 12345


@pytest.mark.asyncio
async def test_pipeline_yandex_unknown_city_raises(monkeypatch):
    """В pipeline._from_browser неизвестный город прерывает выполнение с RuntimeError."""
    import httpx
    from avia.pipeline import _from_browser

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=["unknown", []]))
    monkeypatch.setattr(
        "avia.yandex_ids._build_client",
        lambda **kw: httpx.AsyncClient(transport=transport, **kw),
    )

    settings = load_settings()
    site = next(s for s in settings.sites if s.name == "yandex_travel")
    q = Query("UNKNOWN", "ALA", date(2026, 12, 12))

    with pytest.raises(RuntimeError, match=r"Яндекс Путешествия: не нашёл id для UNKNOWN"):
        await _from_browser(site, q, settings)
