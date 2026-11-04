from __future__ import annotations

from .config import SiteConfig
from .models import Query


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
