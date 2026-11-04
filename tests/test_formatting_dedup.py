from datetime import date

from avia.formatting import format_report, money
from avia.models import Offer, Query, SiteResult


def _offer(price: int, dep: str = "22:50") -> Offer:
    return Offer(site="x", price=price, airline="Nordwind", depart_time=dep, arrive_time="18:25", transfers=1)


def test_identical_offers_shown_once_and_top_n_filled_with_unique():
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offers = [_offer(16403)] * 3 + [_offer(17241, "19:30"), _offer(17500, "14:15")]
    text = format_report(q, [SiteResult("x", True, offers)], top_n=3)
    assert text.count(money(16403, "RUB")) == 2  # строка «Дешевле всего» + один пункт списка
    assert "<b>1.</b>" in text
    assert "<b>2.</b>" in text
    assert "<b>3.</b>" in text
    assert "<b>4.</b>" not in text
    assert money(17500, "RUB") in text
