"""Реестр парсеров. Парсер браузерного сайта: (Snapshot, Query) -> list[Offer].

Чтобы добавить сайт без LLM:
  1. python -m avia.recon <сайт> Москва Алматы 10.12   (сохранит XHR JSON в debug/recon/<сайт>/)
  2. написать функцию-парсер по реальному ответу и повесить @register_parser("имя")
  3. указать parser: имя в config/sites.yaml
"""
from __future__ import annotations

from typing import Callable, Iterable

from ..collector import Snapshot
from ..models import Offer, Query

ParserFn = Callable[[Snapshot, Query], list[Offer]]
PARSERS: dict[str, ParserFn] = {}


class ParsedOffers(list):
    """Офферы разбора и сколько элементов выдачи парсер пропустил (кривые: нет цены, битая структура).

    Политика одна для всех сайтов: кривой элемент пропускается, остальные остаются. Строку WARNING
    пишет pipeline._process_snapshot — одну на полный разбор снимка; быстрый разбор (FAST_STOP)
    зовёт парсер много раз за сбор и ничего не пишет.
    """

    def __init__(self, offers: Iterable[Offer] = (), *, total: int = 0, skipped: int = 0) -> None:
        super().__init__(offers)
        self.total = total
        self.skipped = skipped


def register_parser(name: str):
    def deco(fn: ParserFn) -> ParserFn:
        PARSERS[name] = fn
        return fn

    return deco


# Импорт модулей регистрирует их парсеры.
from . import aviasales  # noqa: E402,F401
from . import city_travel  # noqa: E402,F401
from . import kupibilet  # noqa: E402,F401
from . import ozon_travel  # noqa: E402,F401
from . import travelpayouts  # noqa: E402,F401
from . import ttravel  # noqa: E402,F401
from . import tutu  # noqa: E402,F401
from . import yandex_travel  # noqa: E402,F401
