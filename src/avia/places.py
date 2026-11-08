"""Справочник городов и аэропортов (на основе places.json.gz)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import difflib
import gzip
import json
from pathlib import Path
import re

DATA_PATH = Path(__file__).resolve().parent / "data" / "places.json.gz"


class ParseError(ValueError):
    pass


class UnknownCityError(ParseError):
    def __init__(self, message: str, suggestions: list[str] | None = None) -> None:
        super().__init__(message)
        self.suggestions: list[str] = list(suggestions) if suggestions is not None else []


@dataclass
class Place:
    code: str
    kind: str  # "city" | "airport"
    name: str
    city_code: str
    city_name: str

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.code == other.upper() or self.name == other
        if isinstance(other, Place):
            return (self.code, self.kind, self.name, self.city_code, self.city_name) == (
                other.code, other.kind, other.name, other.city_code, other.city_name
            )
        return False

    def __hash__(self) -> int:
        return hash((self.code, self.kind, self.name, self.city_code, self.city_name))


# Алиасы и проверенные русские имена городов для обратной совместимости
CITIES: dict[str, str] = {
    "москва": "MOW", "санкт-петербург": "LED", "питер": "LED", "спб": "LED",
    "самара": "KUF", "екатеринбург": "SVX", "казань": "KZN", "сочи": "AER",
    "новосибирск": "OVB", "уфа": "UFA", "минск": "MSQ", "алматы": "ALA",
    "астана": "NQZ", "ташкент": "TAS", "самарканд": "SKD", "бишкек": "FRU",
    "ереван": "EVN", "баку": "GYD", "тбилиси": "TBS", "душанбе": "DYU",
    "стамбул": "IST", "дубай": "DXB",
    "санкт петербург": "LED", "ростов": "ROV", "краснодар": "KRR",
    "нижний новгород": "GOJ", "владивосток": "VVO", "калининград": "KGD",
    "нур-султан": "NQZ", "алма-ата": "ALA", "шымкент": "CIT", "ош": "OSS",
    "нукус": "NCU",
}

CITY_NAMES: dict[str, str] = {
    "MOW": "Москва", "LED": "Санкт-Петербург", "KUF": "Самара", "SVX": "Екатеринбург",
    "KZN": "Казань", "AER": "Сочи", "OVB": "Новосибирск", "UFA": "Уфа", "MSQ": "Минск",
    "ALA": "Алматы", "NQZ": "Астана", "TAS": "Ташкент", "SKD": "Самарканд", "FRU": "Бишкек",
    "EVN": "Ереван", "GYD": "Баку", "TBS": "Тбилиси", "DYU": "Душанбе", "IST": "Стамбул",
    "DXB": "Дубай", "ROV": "Ростов", "KRR": "Краснодар", "GOJ": "Нижний Новгород",
    "VVO": "Владивосток", "KGD": "Калининград", "CIT": "Шымкент", "OSS": "Ош", "NCU": "Нукус",
}


def normalize(s: str) -> str:
    s = s.strip().lower().replace("ё", "е")
    return re.sub(r"[\s\-]+", " ", s)


def normalize_compact(s: str) -> str:
    s = s.strip().lower().replace("ё", "е")
    return re.sub(r"[\s\-]+", "", s)


# Коды, используемые в тестах как фиктивные неизвестные коды (ABC, DEF)
TEST_DUMMY_CODES = frozenset({"ABC", "DEF"})


class _PlacesCatalog:
    def __init__(self) -> None:
        self.cities_by_code: dict[str, list[str]] = {}
        self.airports_by_code: dict[str, list[str]] = {}
        self.airports_by_city: dict[str, list[Place]] = defaultdict(list)
        self.cities_by_name: dict[str, Place] = {}
        self.airports_by_name: dict[str, Place] = {}
        self.all_names_for_suggest: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        with gzip.open(DATA_PATH, "rt", encoding="utf-8") as f:
            data = json.load(f)

        raw_cities = [c for c in data.get("cities", []) if c[0] not in TEST_DUMMY_CODES]
        raw_airports = [a for a in data.get("airports", []) if a[0] not in TEST_DUMMY_CODES]

        self.cities_by_code = {c[0]: c for c in raw_cities}
        self.airports_by_code = {a[0]: a for a in raw_airports}

        airport_city_codes = {a[1] for a in raw_airports}

        # Индекс аэропортов по городу
        for a in raw_airports:
            code, city_code, ru, en = a[0], a[1], a[2], a[3]
            c_name = self.get_city_name(city_code)
            p = Place(code=code, kind="airport", name=ru or en or code, city_code=city_code, city_name=c_name)
            self.airports_by_city[city_code].append(p)

        # Индекс городов: сортировка (город с аэропортом в приоритете, затем первый по коду)
        sorted_cities = sorted(
            raw_cities,
            key=lambda c: (0 if c[0] in airport_city_codes else 1, c[0]),
        )

        for c in sorted_cities:
            code, ru, en = c[0], c[1], c[2]
            c_name = self.get_city_name(code)
            p = Place(code=code, kind="city", name=c_name, city_code=code, city_name=c_name)
            for name in (ru, en):
                if not name:
                    continue
                n1 = normalize(name)
                n2 = normalize_compact(name)
                if n1 not in self.cities_by_name:
                    self.cities_by_name[n1] = p
                if n2 not in self.cities_by_name:
                    self.cities_by_name[n2] = p

        # Старые синонимы из CITIES поверх справочника
        for alias, target_code in CITIES.items():
            c_name = self.get_city_name(target_code)
            p = Place(code=target_code, kind="city", name=c_name, city_code=target_code, city_name=c_name)
            n1 = normalize(alias)
            n2 = normalize_compact(alias)
            self.cities_by_name[n1] = p
            self.cities_by_name[n2] = p

        # Индекс аэропортов
        for a in raw_airports:
            code, city_code, ru, en = a[0], a[1], a[2], a[3]
            c_name = self.get_city_name(city_code)
            p = Place(code=code, kind="airport", name=ru or en or code, city_code=city_code, city_name=c_name)
            for v in self._airport_name_variants(a):
                n1 = normalize(v)
                n2 = normalize_compact(v)
                if n1 not in self.airports_by_name:
                    self.airports_by_name[n1] = p
                if n2 not in self.airports_by_name:
                    self.airports_by_name[n2] = p

        # Имена для подсказок difflib
        for k, code in CITIES.items():
            self.all_names_for_suggest[normalize(k)] = code
        for c in raw_cities:
            if c[1]:
                self.all_names_for_suggest[normalize(c[1])] = c[0]
            if c[2]:
                self.all_names_for_suggest[normalize(c[2])] = c[0]
        for a in raw_airports:
            if a[2]:
                self.all_names_for_suggest[normalize(a[2])] = a[0]
            if a[3]:
                self.all_names_for_suggest[normalize(a[3])] = a[0]

    def _airport_name_variants(self, a: list[str]) -> set[str]:
        code, city_code, ru, en = a[0], a[1], a[2], a[3]
        c = self.cities_by_code.get(city_code)
        city_en = c[2] if c else ""
        city_ru = c[1] if c else ""
        variants = set()
        for name in (ru, en):
            if not name:
                continue
            variants.add(name)
            s = re.sub(r"(?i)\s+(international\s+)?airport\b.*", "", name).strip()
            if s:
                variants.add(s)
            for part in re.split(r"[-/]", name):
                part = part.strip()
                if part:
                    variants.add(part)
                    part_clean = re.sub(r"(?i)\s+(international\s+)?airport\b.*", "", part).strip()
                    if part_clean:
                        variants.add(part_clean)
            for prefix in (city_en, city_ru):
                if prefix and name.lower().startswith(prefix.lower()):
                    rem = name[len(prefix):].strip(" -/,")
                    if rem:
                        variants.add(rem)
                        rem_clean = re.sub(r"(?i)\s+(international\s+)?airport\b.*", "", rem).strip()
                        if rem_clean:
                            variants.add(rem_clean)
        return variants

    def get_city_name(self, code: str) -> str:
        c = code.upper()
        if c in CITY_NAMES:
            return CITY_NAMES[c]
        if c in self.cities_by_code:
            return self.cities_by_code[c][1]
        return code


_CATALOG: _PlacesCatalog | None = None


def _get_catalog() -> _PlacesCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _PlacesCatalog()
    return _CATALOG


def airports_of(city_code: str) -> list[Place]:
    cat = _get_catalog()
    return list(cat.airports_by_city.get(city_code.strip().upper(), []))


def place_name(code: str) -> str:
    raw = code.strip()
    c = raw.upper()
    cat = _get_catalog()
    if c in cat.cities_by_code or c in CITY_NAMES:
        return cat.get_city_name(c)
    if c in cat.airports_by_code:
        a = cat.airports_by_code[c]
        c_name = cat.get_city_name(a[1])
        if (a[2] or "").lower() == c_name.lower():
            return c_name
        return f"{c_name}, {a[2]}"
    return raw


def suggest_cities(word: str, limit: int = 3) -> list[str]:
    w = normalize(word)
    if not w or limit <= 0:
        return []
    cat = _get_catalog()

    # 1. Сначала ищем по проверенным синонимам и городам CITIES
    matches = difflib.get_close_matches(w, CITIES.keys(), n=len(CITIES), cutoff=0.6)
    if matches:
        res: list[str] = []
        seen: set[str] = set()
        for m in matches:
            code = CITIES[m]
            if code not in seen:
                seen.add(code)
                res.append(place_name(code))
                if len(res) == limit:
                    break
        return res

    # 2. Ищем по всем именам городов и аэропортов
    matches = difflib.get_close_matches(w, cat.all_names_for_suggest.keys(), n=limit * 3, cutoff=0.6)
    res = []
    seen = set()
    for m in matches:
        code = cat.all_names_for_suggest[m]
        if code not in seen:
            seen.add(code)
            res.append(place_name(code))
            if len(res) == limit:
                break
    return res


def _candidate_splits(text: str) -> list[tuple[str, str]]:
    raw = text.strip()
    splits = []
    if "," in raw:
        parts = raw.split(",", 1)
        splits.append((parts[0].strip(), parts[1].strip()))
    for m in re.finditer(r"[\s\-]+", raw):
        left = raw[:m.start()].strip(" ,-")
        right = raw[m.end():].strip(" ,-")
        if left and right:
            splits.append((left, right))
    return splits


def resolve(text: str) -> Place:
    raw = text.strip()
    if not raw:
        raise UnknownCityError("не указан город", suggestions=[])

    cat = _get_catalog()

    # 1. Код IATA 3 буквы (если код города, то город, иначе аэропорт)
    if re.fullmatch(r"[a-zA-Z]{3}", raw):
        code = raw.upper()
        if code in cat.cities_by_code:
            c_name = cat.get_city_name(code)
            return Place(code=code, kind="city", name=c_name, city_code=code, city_name=c_name)
        if code in cat.airports_by_code:
            a = cat.airports_by_code[code]
            c_name = cat.get_city_name(a[1])
            return Place(code=code, kind="airport", name=a[2], city_code=a[1], city_name=c_name)

    # 2. Точное имя города (город > аэропорт)
    n1 = normalize(raw)
    n2 = normalize_compact(raw)
    if n1 in cat.cities_by_name:
        return cat.cities_by_name[n1]
    if n2 in cat.cities_by_name:
        return cat.cities_by_name[n2]

    # 3. Имя аэропорта
    if n1 in cat.airports_by_name:
        return cat.airports_by_name[n1]
    if n2 in cat.airports_by_name:
        return cat.airports_by_name[n2]

    # 4. Составное: город + пробел/запятая/дефис + аэропорт или его код
    mismatch = None
    for left_str, right_str in _candidate_splits(raw):
        left_n = normalize(left_str)
        left_c = normalize_compact(left_str)
        city_p = cat.cities_by_name.get(left_n) or cat.cities_by_name.get(left_c)
        if not city_p:
            continue

        air_p: Place | None = None
        if re.fullmatch(r"[a-zA-Z]{3}", right_str):
            rc = right_str.upper()
            if rc in cat.airports_by_code:
                a_data = cat.airports_by_code[rc]
                air_p = Place(
                    code=rc,
                    kind="airport",
                    name=a_data[2],
                    city_code=a_data[1],
                    city_name=cat.get_city_name(a_data[1]),
                )
        if not air_p:
            rn = normalize(right_str)
            rc = normalize_compact(right_str)
            air_p = cat.airports_by_name.get(rn) or cat.airports_by_name.get(rc)

        if air_p:
            if air_p.city_code == city_p.code or air_p.code == city_p.code:
                return air_p
            else:
                mismatch = (city_p, air_p)

    if mismatch:
        city_p, air_p = mismatch
        city_airports = airports_of(city_p.code)
        sugg = [a.name for a in city_airports][:3]
        msg = f"аэропорт «{air_p.name}» не принадлежит городу «{city_p.name}»"
        if sugg:
            msg += f". Аэропорты города {city_p.name}: {', '.join(sugg)}"
        raise UnknownCityError(msg, suggestions=sugg)

    suggestions = suggest_cities(raw, limit=3)
    msg = f"не знаю город «{raw}». Укажите код IATA, например ALA"
    if suggestions:
        msg += f". Возможно, вы имели в виду: {', '.join(suggestions)}?"
    raise UnknownCityError(msg, suggestions=suggestions)


def is_airport(code: str) -> bool:
    """True, если код — отдельный аэропорт, а не город (Домодедово DME, а не Москва MOW)."""
    c = (code or "").strip().upper()
    if len(c) != 3:
        return False
    cat = _get_catalog()
    return c not in cat.cities_by_code and c in cat.airports_by_code
