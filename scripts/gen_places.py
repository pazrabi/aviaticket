"""Собирает src/avia/data/places.json.gz из открытых данных Travelpayouts (города и аэропорты).

Запуск: .venv/bin/python scripts/gen_places.py
"""
from __future__ import annotations

import gzip
import json
import urllib.request
from pathlib import Path

BASE = "https://api.travelpayouts.com/data"
OUT = Path(__file__).resolve().parent.parent / "src" / "avia" / "data" / "places.json.gz"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> None:
    cities_ru = _get(f"{BASE}/ru/cities.json")
    airports_ru = _get(f"{BASE}/ru/airports.json")
    cities = []
    for c in cities_ru:
        if not c.get("has_flightable_airport"):
            continue
        cities.append([
            c["code"], c.get("name") or "", (c.get("name_translations") or {}).get("en") or "", c.get("country_code") or "",
        ])
    codes = {c[0] for c in cities}
    airports = []
    for a in airports_ru:
        if a.get("iata_type") != "airport" or not a.get("flightable"):
            continue
        airports.append([
            a["code"], a.get("city_code") or "", a.get("name") or "", (a.get("name_translations") or {}).get("en") or "",
        ])
    data = {"cities": sorted(cities), "airports": sorted(airports)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(len(cities), "городов,", len(airports), "аэропортов ->", OUT, OUT.stat().st_size, "байт")


if __name__ == "__main__":
    main()
