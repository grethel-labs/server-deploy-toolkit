"""Central language management for the server and the web app.

* English is the source language and the default for the API and internal texts.
* Every human-facing text is written once in English, with named placeholders:
  ``tr("Restart {service}", service=name)``. Translations live in one catalog per
  language, ``opstend/locales/<lang>.json``, keyed by the English template. The web
  app loads the same catalogs from ``/api/i18n/<lang>``, so adding a language means
  adding one file.
* ``tr()`` returns an ``I18nStr``: the text in the current request's language that
  still knows its template and parameters. Texts that are stored (job titles, step
  effects, findings) keep that template, and the API renders them again in each
  reader's language.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from functools import cache
from pathlib import Path
from typing import Any

DEFAULT = "en"
LOCALES = Path(__file__).parent / "locales"
current: ContextVar[str] = ContextVar("opstend_lang", default=DEFAULT)


@cache
def catalog(lang: str) -> dict[str, str]:
    if lang == DEFAULT:
        return {}
    path = LOCALES / f"{lang}.json"
    if not path.is_file():
        return {}
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return data


def languages() -> list[str]:
    return [DEFAULT, *sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != DEFAULT)]


class I18nStr(str):
    """A rendered text that remembers how to render itself in another language."""

    template: str
    params: dict[str, Any]

    def __new__(cls, rendered: str, template: str, params: dict[str, Any]) -> I18nStr:
        obj = super().__new__(cls, rendered)
        obj.template = template
        obj.params = params
        return obj

    def meta(self) -> dict[str, Any]:
        return {"t": self.template, "p": {k: _plain(v) for k, v in self.params.items()}}


def _plain(value: Any) -> Any:
    if isinstance(value, I18nStr):
        return {"__i18n__": value.meta()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _param(value: Any, lang: str) -> Any:
    if isinstance(value, dict) and "__i18n__" in value:
        return render(value["__i18n__"], lang)
    if isinstance(value, I18nStr):
        return render(value.meta(), lang)
    return value


def translate(template: str, lang: str | None = None) -> str:
    return catalog(lang or current.get()).get(template, template)


def render(meta: dict[str, Any], lang: str | None = None) -> str:
    lang = lang or current.get()
    params = {k: _param(v, lang) for k, v in meta.get("p", {}).items()}
    try:
        return translate(meta["t"], lang).format(**params)
    except (KeyError, IndexError, ValueError):
        return str(meta["t"]).format(**params)


def tr(template: str, **params: Any) -> str:
    """Translate `template` into the current language and fill in the parameters."""
    return I18nStr(render({"t": template, "p": params}), template, params)


def pick(accept_language: str | None, preferred: str | None = None) -> str:
    available = languages()
    if preferred in available:
        return preferred
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in available:
            return code
    return DEFAULT


# --- stored texts -------------------------------------------------------------------


def capture(fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Templates of every I18nStr among `fields`, for storing next to the text."""
    return {k: v.meta() for k, v in fields.items() if isinstance(v, I18nStr)}


def localize(data: Any, lang: str | None = None) -> Any:
    """Render stored texts (objects with an `i18n` map) in the reader's language."""
    lang = lang or current.get()
    if isinstance(data, list):
        return [localize(x, lang) for x in data]
    if isinstance(data, I18nStr):
        return render(data.meta(), lang)
    if isinstance(data, dict):
        out = {k: localize(v, lang) for k, v in data.items() if k != "i18n"}
        for field, meta in (data.get("i18n") or {}).items():
            if field in out:
                out[field] = render(meta, lang)
        return out
    return data
