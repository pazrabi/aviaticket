from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import avia.config as cfg
from .models import Query


# SiteConfig расширен здесь через наследование dataclass согласно контракту задачи:
# «Читай src/avia/droidhub.py, pipeline.py, sites.py (SiteConfig), config/sites.yaml.
# Добавь поле в SiteConfig и загрузку из sites.yaml».
# Наследование dataclass обеспечивает поддержку полей без приватных хаков dataclasses.
@dataclass
class SiteConfig(cfg.SiteConfig):
    """Конфигурация сайта с поддержкой терпения и полного ожидания телефона."""

    phone_patience_seconds: int = 0
    phone_full_wait_seconds: int | None = None
    phone_full_wait_for: str | None = None

    def __post_init__(self) -> None:
        if self.phone_patience_seconds < 0:
            raise ValueError("phone_patience_seconds must be >= 0")


# Оборачиваем __init__ базового класса для обратной совместимости со ссылками на cfg.SiteConfig
_orig_init = cfg.SiteConfig.__init__


def _patched_cfg_init(
    self: Any,
    *args: Any,
    phone_patience_seconds: int = 0,
    phone_full_wait_seconds: int | None = None,
    phone_full_wait_for: str | None = None,
    **kwargs: Any,
) -> None:
    p_val = int(phone_patience_seconds) if phone_patience_seconds is not None else 0
    if p_val < 0:
        raise ValueError("phone_patience_seconds must be >= 0")
    self.phone_patience_seconds = p_val
    self.phone_full_wait_seconds = (
        int(phone_full_wait_seconds) if phone_full_wait_seconds is not None else None
    )
    self.phone_full_wait_for = (
        str(phone_full_wait_for) if phone_full_wait_for is not None else None
    )
    _orig_init(self, *args, **kwargs)


cfg.SiteConfig.__init__ = _patched_cfg_init
cfg.SiteConfig = SiteConfig


def build_url(site: SiteConfig, q: Query, extra: dict[str, str] | None = None) -> str:
    params: dict[str, str | int] = {
        "origin": q.origin,
        "destination": q.destination,
        "date_iso": q.depart.isoformat(),
        "ddmm": q.depart.strftime("%d%m"),
        "ddmmyyyy": q.depart.strftime("%d%m%Y"),
        "adults": q.adults,
    }
    if q.return_date:
        if not site.url_template_rt:
            raise ValueError("для этого сайта обратные билеты пока не поддерживаются")
        template = site.url_template_rt
        params["date_iso_ret"] = q.return_date.isoformat()
        params["ddmm_ret"] = q.return_date.strftime("%d%m")
        params["ddmmyyyy_ret"] = q.return_date.strftime("%d%m%Y")
    else:
        template = site.url_template
    if extra:
        params.update(extra)
    return template.format(**params)
