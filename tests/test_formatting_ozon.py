from datetime import date

from avia.formatting import build_report, ozon_url
from avia.models import Offer, Query, SiteResult


def _q(return_date=None):
    return Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1), adults=1, return_date=return_date)


def test_ozon_url_round_trip():
    url = ozon_url(_q(date(2026, 12, 4)))
    assert url == (
        "https://www.ozon.ru/travel/flight/search?Children=0&Dlts=1&Infants=0"
        "&ServiceClass=ECONOMY&dates=d2026-12-01d2026-12-04&route=kufevnevnkuf"
    )


def test_ozon_url_one_way():
    url = ozon_url(_q())
    assert "dates=d2026-12-01&" in url
    assert url.endswith("&route=kufevn")


def test_report_has_ozon_link_without_offers():
    q = _q()
    chunks, _ = build_report(q, [SiteResult(site="x", ok=True, offers=[])])
    text = "\n".join(chunks)
    assert "Ozon Travel" in text
    assert "route=kufevn" in text


def test_report_has_ozon_link_with_offers_and_fits():
    q = _q(date(2026, 12, 4))
    offers = [Offer(site="x", price=1000 + i, currency="RUB", airline="A", url="https://e.x/%d" % i) for i in range(50)]
    chunks, flat = build_report(q, [SiteResult(site="x", ok=True, offers=offers, elapsed=1.0)], top_n=50)
    assert all(len(c) <= 4096 for c in chunks)
    assert "route=kufevnevnkuf" in chunks[-1]
    assert len(flat) > 0


def test_failed_site_gets_link():
    from avia.formatting import build_compact_report
    from avia.criteria import SearchCriteria
    from avia.models import SiteResult

    r = SiteResult(site="tutu", ok=False, offers=[], error="капча", page_url="https://tutu.ru/x?a=1&b=2")
    chunks, _, _ = build_compact_report(_q(), [r], SearchCriteria(), baggage_mode="any",
                                        dropped_baggage=0, dropped_criteria=0)
    text = "\n".join(chunks)
    assert 'href="https://tutu.ru/x?a=1&amp;b=2"' in text


def test_no_ozon_footer_when_ozon_searched():
    from avia.criteria import SearchCriteria
    from avia.formatting import build_compact_report
    from avia.models import SiteResult

    r = SiteResult(site="ozon_travel", ok=False, offers=[], error="капча", page_url="https://www.ozon.ru/x")
    chunks, _, _ = build_compact_report(_q(), [r], SearchCriteria(), baggage_mode="any",
                                        dropped_baggage=0, dropped_criteria=0)
    assert "цены Озона бот не проверяет" not in "\n".join(chunks)
