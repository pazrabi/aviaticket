"""Travelpayouts / Aviasales Data API v3: prices_for_dates.

Это кэш поисков Aviasales за последние ~48 часов, а не живой поиск. Зато нужен только
токен (бесплатно), без браузера и без разбора вёрстки.
Документация: support.travelpayouts.com, статья «Aviasales Data API».
"""
from __future__ import annotations

from ..models import Offer, Query

ENDPOINT = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SITE_URL = "https://www.aviasales.ru"


def format_duration(minutes) -> str | None:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    return f"{m // 60}ч {m % 60:02d}м"


def search_url(q: Query, marker: str = "") -> str:
    url = f"{SITE_URL}/search/{q.origin}{q.depart:%d%m}{q.destination}{q.adults}"
    return f"{url}?marker={marker}" if marker else url


def parse_prices_for_dates(payload: dict, q: Query, marker: str = "") -> list[Offer]:
    """Чистая функция: JSON ответа -> список Offer (легко тестировать без сети)."""
    if not isinstance(payload, dict) or not payload.get("success", True):
        err = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(f"API вернул ошибку: {err or 'success=false'}")
    currency = str(payload.get("currency") or q.currency).upper()
    offers: list[Offer] = []
    for item in payload.get("data") or []:
        try:
            price = int(float(item["price"])) * q.adults
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        departure = str(item.get("departure_at") or "")
        link = item.get("link")
        url = f"{SITE_URL}{link}" if isinstance(link, str) and link.startswith("/") else search_url(q, marker)
        if marker and "marker=" not in url:
            url += ("&" if "?" in url else "?") + f"marker={marker}"
        transfers = item.get("transfers")
        offers.append(
            Offer(
                site="aviasales (кэш)",
                price=price,
                currency=currency,
                airline=item.get("airline") or None,
                depart_time=departure[11:16] if len(departure) >= 16 else None,
                duration=format_duration(item.get("duration")),
                transfers=int(transfers) if isinstance(transfers, (int, float)) else None,
                url=url,
            )
        )
    offers.sort(key=lambda o: o.price)
    return offers


def query_params(q: Query, limit: int = 10) -> dict[str, str | int]:
    """Параметры prices_for_dates. Туда-обратно (П32) — return_at и one_way=false: ответ того же вида,
    его разбирает та же parse_prices_for_dates (цена — за всю поездку). В одну сторону — как было."""
    params: dict[str, str | int] = {
        "origin": q.origin,
        "destination": q.destination,
        "departure_at": q.depart.isoformat(),
        "one_way": "true",
        "sorting": "price",
        "currency": q.currency.lower(),
        "limit": limit,
        "page": 1,
    }
    if q.return_date is None:
        return params
    return {**params, "one_way": "false", "return_at": q.return_date.isoformat()}


async def fetch_travelpayouts(q: Query, token: str, marker: str = "", limit: int = 10) -> list[Offer]:
    if not token:
        raise RuntimeError("не задан TP_TOKEN (токен из кабинета Travelpayouts, раздел API)")
    import httpx  # лениво: чистый разбор ответа не требует сети и httpx

    params = query_params(q, limit)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(ENDPOINT, params=params, headers={"X-Access-Token": token})
    if r.status_code == 401:
        raise RuntimeError("токен Travelpayouts не принят (401)")
    r.raise_for_status()
    return parse_prices_for_dates(r.json(), q, marker)
