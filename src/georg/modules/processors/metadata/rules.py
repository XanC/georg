# SPDX-License-Identifier: AGPL-3.0-only
"""`MetadataGenerator` that derives metadata from the source item and configured defaults.

Companion documents in `SourceItem.hints["documents"]` (names as strings, or mappings
with a "name" key) are parsed by `parse_document_name`. A document is used only if its
date matches the service date, or if the service date is otherwise unknown.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.errors import ConfigError
from georg.core.types import MetadataContext, SermonMetadata

_EXTENSION = re.compile(r"\.(?:docx?|odt|pdf|rtf|txt|pages|gdoc)$", re.IGNORECASE)
_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[ _]+(?P<rest>.*))?$")
_PART_SEPARATOR = re.compile(r"\s+-\s+")


@dataclass(frozen=True)
class DocumentHint:
    service_date: date
    occasion: str | None
    setting: str | None
    """E.g. "DS1"; not part of `SermonMetadata`, kept for callers that want it."""
    preacher: str | None


def _clean(text: str) -> str | None:
    return " ".join(text.split()) or None


def parse_document_name(name: str) -> DocumentHint | None:
    """Parse names like "2026-11-01_All_Saints__Day_-_DS1-Eric.docx".

    Layout: `<YYYY-MM-DD>_<occasion>[_-_<setting>[-<preacher>]]`, underscores standing
    for spaces. A lone part after the separator is a setting if it contains a digit,
    otherwise a preacher. Returns None if the name does not start with a valid date.
    """
    stem = _EXTENSION.sub("", name.strip().rsplit("/", 1)[-1])
    match = _NAME.match(stem)
    if match is None:
        return None
    try:
        when = date.fromisoformat(match.group("date"))
    except ValueError:
        return None
    rest = (match.group("rest") or "").replace("_", " ")
    occasion_part, *tail_parts = _PART_SEPARATOR.split(rest, maxsplit=1)
    setting = preacher = None
    if tail_parts:
        head, dash, tail = tail_parts[0].partition("-")
        if dash:
            setting, preacher = _clean(head), _clean(tail)
        elif re.search(r"\d", head):
            setting = _clean(head)
        else:
            preacher = _clean(head)
    return DocumentHint(
        service_date=when, occasion=_clean(occasion_part), setting=setting, preacher=preacher
    )


def _document_names(hints: Mapping[str, Any]) -> list[str]:
    docs = hints.get("documents") or []
    if isinstance(docs, str | Mapping):
        docs = [docs]
    names: list[str] = []
    for doc in docs if isinstance(docs, list | tuple) else []:
        name = doc.get("name") if isinstance(doc, Mapping) else doc
        if isinstance(name, str):
            names.append(name)
    return names


class MetadataOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    preachers: dict[str, str] = Field(default_factory=dict)
    """Maps preacher hints from document names (case-insensitive) to display names."""


class RulesMetadataGenerator:
    """Module type `rules`."""

    version = "rules-1"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        try:
            self.options = MetadataOptions.model_validate(dict(options or {}))
        except ValidationError as exc:
            raise ConfigError(f"metadata options: {exc}") from exc
        self._preachers = {k.casefold(): v for k, v in self.options.preachers.items()}

    def generate(self, ctx: MetadataContext) -> SermonMetadata:
        service_date = ctx.source.service_date
        hint: DocumentHint | None = None
        for name in sorted(_document_names(ctx.source.hints)):
            parsed = parse_document_name(name)
            if parsed and (service_date is None or parsed.service_date == service_date):
                hint = parsed
                break
        if service_date is None and hint is not None:
            service_date = hint.service_date
        update: dict[str, Any] = {
            "service_date": service_date or ctx.defaults.service_date,
            "extra": dict(ctx.defaults.extra),
        }
        if hint is not None and hint.occasion:
            update["occasion"] = hint.occasion
        if hint is not None and hint.preacher:
            update["preacher"] = self._preachers.get(hint.preacher.casefold(), hint.preacher)
        return ctx.defaults.model_copy(update=update)
