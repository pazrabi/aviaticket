"""Тесты функции build_url и плейсхолдеров дат с точками dd_mm_yyyy."""
from __future__ import annotations

from datetime import date
import pytest

from avia.config import SiteConfig, load_settings
from avia.models import Query
from avia.sites import build_url


def test_build_url_dd_mm_yyyy_placeholders():
    """Проверка плейсхолдеров dd_mm_yyyy и dd_mm_yyyy_ret с точками."""
    site = SiteConfig(
        name="test_dotted",
        url_template="https://example.com/search/{origin}/{destination}/{dd_mm_yyyy}/{adults}",
        url_template_rt="https://example.com/search/{origin}/{destination}/{dd_mm_yyyy}/{dd_mm_yyyy_ret}/{adults}",
    )

    # В одну сторону
    q_ow = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 20), adults=1)
    url_ow = build_url(site, q_ow)
    assert url_ow == "https://example.com/search/KUF/EVN/20.12.2026/1"

    # Туда-обратно
    q_rt = Query(
        origin="KUF",
        destination="MOW",
        depart=date(2026, 12, 12),
        return_date=date(2026, 12, 15),
        adults=2,
    )
    url_rt = build_url(site, q_rt)
    assert url_rt == "https://example.com/search/KUF/MOW/12.12.2026/15.12.2026/2"


def test_build_url_city_travel_templates_from_config():
    """Проверка шаблонов city_travel из config/sites.yaml."""
    settings = load_settings()
    site = next((s for s in settings.sites if s.name == "city_travel"), None)
    assert site is not None

    q_ow = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 20), adults=1)
    url_ow = build_url(site, q_ow)
    assert (
        url_ow
        == "https://city.travel/avia/search/KUF/EVN/E/20.12.2026/0/1-0-0/0/0/0/00000000000000000000000000000000"
    )

    q_rt = Query(
        origin="KUF",
        destination="MOW",
        depart=date(2026, 12, 12),
        return_date=date(2026, 12, 15),
        adults=1,
    )
    url_rt = build_url(site, q_rt)
    assert (
        url_rt
        == "https://city.travel/avia/search/KUF/MOW/E/12.12.2026/15.12.2026/1-0-0/0/0/0/00000000000000000000000000000000?rt=RTAMOW"
    )
