"""Реестр парсеров. Парсер браузерного сайта: (Snapshot, Query) -> list[Offer].

Чтобы добавить сайт без LLM:
  1. python -m avia.recon <сайт> Москва Алматы 10.12   (сохранит XHR JSON в debug/recon/<сайт>/)
  2. написать функцию-парсер по реальному ответу и повесить @register_parser("имя")
  3. указать parser: имя в config/sites.yaml
"""
from __future__ import annotations

from typing import Callable

from ..collector import Snapshot
from ..models import Offer, Query

ParserFn = Callable[[Snapshot, Query], list[Offer]]
PARSERS: dict[str, ParserFn] = {}


def register_parser(name: str):
    def deco(fn: ParserFn) -> ParserFn:
        PARSERS[name] = fn
        return fn

    return deco


# Импорт модулей регистрирует их парсеры.
from . import aviasales  # noqa: E402,F401
from . import kupibilet  # noqa: E402,F401
from . import ozon_travel  # noqa: E402,F401
from . import travelpayouts  # noqa: E402,F401
from . import ttravel  # noqa: E402,F401
from . import tutu  # noqa: E402,F401
from . import yandex_travel  # noqa: E402,F401
