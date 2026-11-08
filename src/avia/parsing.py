"""Разбор команды пользователя: /price Москва Алматы 10.12 [2]"""
from __future__ import annotations

from datetime import date
import re

from .models import Query
from .places import (
    CITIES,
    CITY_NAMES,
    ParseError,
    Place,
    UnknownCityError,
    airports_of,
    place_name,
    resolve,
)
from .places import suggest_cities as places_suggest_cities


def city_name(code: str) -> str:
    return place_name(code)


def suggest_cities(word: str, limit: int = 3) -> list[str]:
    return places_suggest_cities(word, limit=limit)


def to_iata(word: str) -> str:
    return resolve(word).code


def _parse_date_parts(s: str) -> tuple[int, int, int | None]:
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return int(m[3]), int(m[2]), int(m[1])
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?", s)
    if not m:
        raise ParseError(f"не понял дату «{s}». Формат: 10.12 или 2026-12-10")
    day, month = int(m[1]), int(m[2])
    year = None
    if m[3]:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    return day, month, year


def parse_date(s: str, today: date | None = None) -> date:
    today = today or date.today()
    day, month, year = _parse_date_parts(s)
    has_year = year is not None
    if year is None:
        year = today.year
    try:
        d = date(year, month, day)
    except ValueError as e:
        raise ParseError(f"такой даты нет: {s}") from e
    if not has_year and d < today:
        try:
            d = date(year + 1, month, day)
        except ValueError as e:
            raise ParseError(f"такой даты нет: {s}") from e
    if d < today:
        raise ParseError("эта дата уже в прошлом, введите будущую дату")
    return d


def parse_return_date(s: str | date, depart: date, today: date | None = None) -> date:
    today = today or date.today()
    if isinstance(s, date):
        d = s
    else:
        day, month, year = _parse_date_parts(s)
        has_year = year is not None
        if year is None:
            year = depart.year
        try:
            d = date(year, month, day)
        except ValueError as e:
            raise ParseError(f"такой даты нет: {s}") from e
        if not has_year and d < depart:
            try:
                d = date(year + 1, month, day)
            except ValueError as e:
                raise ParseError(f"такой даты нет: {s}") from e
    if d < depart:
        raise ParseError("обратная дата раньше даты вылета")
    if d < today:
        raise ParseError("эта дата уже в прошлом, введите будущую дату")
    return d


def parse_passengers(s: str) -> int:
    s = str(s).strip()
    if s.isdigit() and 1 <= int(s) <= 9:
        return int(s)
    raise ParseError("число пассажиров: от 1 до 9")


def parse_query(text: str, today: date | None = None) -> Query:
    """Принимает аргументы после команды: «Москва Алматы 10.12 2»."""
    args = text.split()
    if len(args) < 3:
        raise ParseError("Формат: /price Москва Алматы 10.12 [пассажиров]")
    adults = 1
    if len(args) >= 4:
        adults = parse_passengers(args[3])
    o, d = to_iata(args[0]), to_iata(args[1])
    if o == d:
        raise ParseError("города вылета и прилёта совпадают")
    return Query(o, d, parse_date(args[2], today), adults)
