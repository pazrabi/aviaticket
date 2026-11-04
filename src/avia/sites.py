from __future__ import annotations

import dataclasses
from typing import Any

import avia.config as cfg
from .models import Query


def _patch_site_config() -> None:
    """Расширяет SiteConfig новыми полями терпения и полного ожидания."""
    cls = cfg.SiteConfig
    if getattr(cls, "_patience_patched", False):
        return

    orig_init = cls.__init__

    def __init__(
        self: Any,
        name: str,
        url_template: str,
        enabled: bool = True,
        wait_selector: str | None = None,
        settle_ms: int = 8000,
        scrolls: int = 3,
        note: str = "",
        source: str = "browser",
        parser: str = "",
        use_llm: bool = False,
        id_resolver: str = "",
        url_template_rt: str = "",
        replay_post: dict | None = None,
        phone: str = "",
        phone_capture_json: list[str] | None = None,
        phone_wait_seconds: int = 40,
        phone_wait_for: str = "",
        phone_patience_seconds: int = 0,
        phone_full_wait_seconds: int | None = None,
        phone_full_wait_for: str | None = None,
        **kwargs: Any,
    ) -> None:
        if phone_patience_seconds is not None:
            p_val = int(phone_patience_seconds)
            if p_val < 0:
                raise ValueError("phone_patience_seconds must be >= 0")
            self.phone_patience_seconds = p_val
        else:
            self.phone_patience_seconds = 0

        self.phone_full_wait_seconds = (
            int(phone_full_wait_seconds) if phone_full_wait_seconds is not None else None
        )
        self.phone_full_wait_for = (
            str(phone_full_wait_for) if phone_full_wait_for is not None else None
        )

        orig_init(
            self,
            name=name,
            url_template=url_template,
            enabled=enabled,
            wait_selector=wait_selector,
            settle_ms=settle_ms,
            scrolls=scrolls,
            note=note,
            source=source,
            parser=parser,
            use_llm=use_llm,
            id_resolver=id_resolver,
            url_template_rt=url_template_rt,
            replay_post=replay_post,
            phone=phone,
            phone_capture_json=phone_capture_json if phone_capture_json is not None else [],
            phone_wait_seconds=phone_wait_seconds,
            phone_wait_for=phone_wait_for,
            **kwargs,
        )

    cls.__init__ = __init__
    cls.phone_patience_seconds = 0
    cls.phone_full_wait_seconds = None
    cls.phone_full_wait_for = None
    cls._patience_patched = True

    for fname, ftype, fdef in [
        ("phone_patience_seconds", int, 0),
        ("phone_full_wait_seconds", int | None, None),
        ("phone_full_wait_for", str | None, None),
    ]:
        fld = dataclasses.Field(fdef, dataclasses.MISSING, True, True, None, True, None, False)
        fld.name = fname
        fld.type = ftype
        fld._field_type = dataclasses._FIELD
        cls.__dataclass_fields__[fname] = fld


_patch_site_config()

SiteConfig = cfg.SiteConfig


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
