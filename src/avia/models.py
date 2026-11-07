from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Query:
    origin: str
    destination: str
    depart: date
    adults: int = 1
    currency: str = "RUB"
    return_date: date | None = None


@dataclass
class Leg:
    airline: str | None = None
    depart_time: str | None = None
    arrive_time: str | None = None
    duration: str | None = None
    transfers: int | None = None
    flight_numbers: list[str] | None = None
    aircraft: str | None = None
    layovers: list[str] | None = None
    depart_at: str | None = None
    arrive_at: str | None = None


@dataclass
class OfferAlt:
    site: str
    price: int
    currency: str = "RUB"
    url: str | None = None
    url_is_direct: bool = False


@dataclass
class Offer:
    site: str
    price: int
    currency: str = "RUB"
    airline: str | None = None
    depart_time: str | None = None
    arrive_time: str | None = None
    duration: str | None = None
    transfers: int | None = None
    baggage: str | None = None
    url: str | None = None
    offer_id: str = ""
    flight_numbers: list[str] | None = None
    aircraft: str | None = None
    layovers: list[str] | None = None
    depart_at: str | None = None
    arrive_at: str | None = None
    carry_on: str | None = None
    fetched_at: float | None = None
    back: Leg | None = None
    url_is_direct: bool = False
    prev_price: int | None = None   # цена этого билета при прошлом наблюдении
    prev_at: float | None = None    # когда она была снята (epoch)
    min_price: int | None = None    # минимальная цена этого билета за прошлые наблюдения
    alts: list[OfferAlt] | None = None
    ticket_hash: str | None = None


@dataclass
class SiteResult:
    site: str
    ok: bool
    offers: list[Offer]
    error: str | None = None
    page_url: str | None = None
    elapsed: float = 0.0
    partial: bool = False
    cached: bool = False
    captcha: bool = False
    device: str | None = None
    queue_wait: float | None = None
    fetch_seconds: float | None = None
    priority: int | None = None
    cached_age: float | None = None
