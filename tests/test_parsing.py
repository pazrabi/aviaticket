from datetime import date
import pytest

from avia.parsing import (
    CITIES,
    CITY_NAMES,
    ParseError,
    UnknownCityError,
    city_name,
    parse_date,
    parse_passengers,
    parse_query,
    parse_return_date,
    suggest_cities,
    to_iata,
)

TODAY = date(2026, 11, 1)


def test_parse_query_with_airports_and_cities():
    # Город и аэропорт
    q1 = parse_query("MOW DME 10.12", today=TODAY)
    assert q1.origin == "MOW"
    assert q1.destination == "DME"
    assert q1.adults == 1

    # Два города
    q2 = parse_query("Москва Алматы 10.12 2", today=TODAY)
    assert q2.origin == "MOW"
    assert q2.destination == "ALA"
    assert q2.adults == 2

    # Совпадающие города
    with pytest.raises(ParseError, match="города вылета и прилёта совпадают"):
        parse_query("MOW MOW 10.12", today=TODAY)


def test_parsing_compatibility():
    # CITIES и CITY_NAMES доступны
    assert isinstance(CITIES, dict)
    assert isinstance(CITY_NAMES, dict)
    assert CITIES["москва"] == "MOW"
    assert CITY_NAMES["MOW"] == "Москва"

    # city_name для известных городов и аэропортов
    assert city_name("MOW") == "Москва"
    assert "Домодедово" in city_name("DME")
    assert city_name("XYZ123") == "XYZ123"

    # suggest_cities
    suggestions = suggest_cities("москав")
    assert "Москва" in suggestions
