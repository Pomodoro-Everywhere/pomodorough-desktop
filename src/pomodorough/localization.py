from __future__ import annotations

import json
import os
from importlib.resources import files
from string import Formatter
from typing import Any

_SUPPORTED = {"en", "ar-XB"}


class Strings:
    """Resource-backed catalog with English fallback and an RTL pseudolocale."""

    def __init__(self, locale: str | None = None) -> None:
        requested = (locale or os.environ.get("POMODOROUGH_LOCALE") or "en").replace(
            "_", "-"
        )
        self.locale = requested if requested in _SUPPORTED else "en"
        english = _load_catalog("en")
        if self.locale == "en":
            self.messages = english
            return

        localized = _load_catalog(self.locale)
        if localized.get("meta.generate_from") == "en":
            direction = localized["meta.direction"]
            localized = {
                key: (_pseudolocalize(value) if key != "meta.direction" else direction)
                for key, value in english.items()
            }
        if set(localized) != set(english):
            raise ValueError(f"locale {self.locale} does not match the English catalog")
        for key, value in localized.items():
            if _fields(value) != _fields(english[key]):
                raise ValueError(f"locale {self.locale} has invalid fields for {key}")
        self.messages: dict[str, str] = localized

    @property
    def is_rtl(self) -> bool:
        return self.messages["meta.direction"] == "rtl"

    def text(self, key: str, **values: Any) -> str:
        return self.messages[key].format(**values)

    def plural(self, key: str, count: int, **values: Any) -> str:
        form = "one" if count == 1 else "other"
        return self.text(f"{key}.{form}", count=count, **values)


def _load_catalog(locale: str) -> dict[str, str]:
    resource = files("pomodorough.resources").joinpath(f"strings.{locale}.json")
    document = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in document.items()
    ):
        raise ValueError(f"locale {locale} must contain a string mapping")
    return document


def _pseudolocalize(value: str) -> str:
    """Decorate/expand text while preserving named-format placeholders verbatim."""
    if not value:
        return value
    parts: list[str] = []
    accents = str.maketrans("AEIOUaeiou", "ÅËÏØÜåëïøü")
    for literal, field, spec, conversion in Formatter().parse(value):
        parts.append(literal.translate(accents))
        if field is not None:
            placeholder = "{" + field
            if conversion:
                placeholder += f"!{conversion}"
            if spec:
                placeholder += f":{spec}"
            parts.append(placeholder + "}")
    return "⟦‼ " + "".join(parts) + " ‼⟧"


def _fields(value: str) -> set[str]:
    return {
        field_name
        for _literal, field_name, _format_spec, _conversion in Formatter().parse(value)
        if field_name
    }
