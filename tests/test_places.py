import pytest

from avia.parsing import UnknownCityError, city_name, to_iata
from avia.places import Place, airports_of, place_name, resolve


def test_places_resolution():
    # Москва→MOW
    p = resolve("Москва")
    assert p.code == "MOW"
    assert p.kind == "city"
    assert to_iata("Москва") == "MOW"

    # «южно-сахалинск»→UUS
    p = resolve("южно-сахалинск")
    assert p.code == "UUS"
    assert to_iata("южно-сахалинск") == "UUS"

    # «Рим»→ROM
    p = resolve("Рим")
    assert p.code == "ROM"
    assert to_iata("Рим") == "ROM"

    # «rome»→ROM
    p = resolve("rome")
    assert p.code == "ROM"
    assert to_iata("rome") == "ROM"

    # «Москва Домодедово»→DME
    p = resolve("Москва Домодедово")
    assert p.code == "DME"
    assert p.kind == "airport"
    assert to_iata("Москва Домодедово") == "DME"

    # «москва, внуково»→VKO
    p = resolve("москва, внуково")
    assert p.code == "VKO"
    assert p.kind == "airport"
    assert to_iata("москва, внуково") == "VKO"

    # «DME»→DME
    p = resolve("DME")
    assert p.code == "DME"
    assert p.kind == "airport"
    assert to_iata("DME") == "DME"

    # «MOW»→MOW
    p = resolve("MOW")
    assert p.code == "MOW"
    assert p.kind == "city"
    assert to_iata("MOW") == "MOW"

    # «Внуково»→VKO
    p = resolve("Внуково")
    assert p.code == "VKO"
    assert p.kind == "airport"
    assert to_iata("Внуково") == "VKO"

    # «Питер»→LED
    p = resolve("Питер")
    assert p.code == "LED"
    assert to_iata("Питер") == "LED"

    # «Heathrow»→LHR, «Фьюмичино»→FCO
    p = resolve("Heathrow")
    assert p.code == "LHR"
    assert to_iata("Heathrow") == "LHR"

    p = resolve("Фьюмичино")
    assert p.code == "FCO"
    assert to_iata("Фьюмичино") == "FCO"

    # «Москва DME»→DME, «Рим Чампино»→CIA
    p = resolve("Москва DME")
    assert p.code == "DME"
    assert to_iata("Москва DME") == "DME"

    p = resolve("Рим Чампино")
    assert p.code == "CIA"
    assert to_iata("Рим Чампино") == "CIA"


def test_invalid_city_airport_combination():
    # «Рим Домодедово»→ошибка
    with pytest.raises(UnknownCityError) as exc_info:
        resolve("Рим Домодедово")
    assert "не принадлежит" in str(exc_info.value)

    with pytest.raises(UnknownCityError):
        to_iata("Рим Домодедово")


def test_typo_suggestion():
    # опечатка «Москав» даёт подсказку Москва
    with pytest.raises(UnknownCityError) as exc_info:
        resolve("Москав")
    assert "Москва" in exc_info.value.suggestions

    with pytest.raises(UnknownCityError) as exc_info2:
        to_iata("Москав")
    assert "Москва" in exc_info2.value.suggestions


def test_city_name_airport_formatting():
    # city_name('DME') содержит «Домодедово»
    dme_name = city_name("DME")
    assert "Домодедово" in dme_name
    assert "Москва" in dme_name
    assert "Домодедово" in place_name("DME")


def test_airports_of_mow():
    # airports_of('MOW') содержит DME, SVO, VKO
    mow_airports = airports_of("MOW")
    assert "DME" in mow_airports
    assert "SVO" in mow_airports
    assert "VKO" in mow_airports

    codes = [a.code for a in mow_airports]
    assert "DME" in codes
    assert "SVO" in codes
    assert "VKO" in codes
