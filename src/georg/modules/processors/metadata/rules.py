# SPDX-License-Identifier: AGPL-3.0-only
"""`MetadataGenerator` that derives metadata from the source item and configured defaults.

Companion documents in `SourceItem.hints["documents"]` (names as strings, or mappings
with a "name" key) are parsed by `parse_document_name`. A document is used only if its
date matches the service date, or if the service date is otherwise unknown.

The pipeline also passes the documents' text (`MetadataContext.documents`). A bulletin
that starts with the occasion and the date ("Twenty-third Sunday after Trinity",
"November 8, 2026") gives the occasion, preferred over the one in the file name. With
the `scripture` option, the sermon's Scripture is detected (see `scripture`).
"""

import logging
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from georg.core.errors import ConfigError
from georg.core.types import MetadataContext, MetadataProposal, SermonMetadata
from georg.modules.processors.format.llama_cpp import LlamaLike, LlamaModelOptions
from georg.modules.processors.metadata.calendar import Calendar
from georg.modules.processors.metadata.readings import parse_header
from georg.modules.processors.metadata.scripture import ScriptureDetector, ScriptureOptions

log = logging.getLogger(__name__)

_EXTENSION = re.compile(r"\.(?:docx?|odt|pdf|rtf|txt|pages|gdoc)$", re.IGNORECASE)
_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[ _]+(?P<rest>.*))?$")
_PART_SEPARATOR = re.compile(r"[\s_]+-[\s_]+")
_APOSTROPHE = re.compile(r"(?<=[^\W_])__(?=[^\W_])")
_HH_MM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
TITLE_FIELDS = frozenset({"occasion", "preacher", "date"})


@dataclass(frozen=True)
class DocumentHint:
    service_date: date
    occasion: str | None
    setting: str | None
    """E.g. "DS1"; not part of `SermonMetadata`, kept for callers that want it."""
    preacher: str | None


def _clean(text: str) -> str | None:
    return " ".join(text.replace("_", " ").split()) or None


def _clean_occasion(text: str) -> str | None:
    # File names cannot hold every character, so "All Saints' Day" is saved as
    # "All_Saints__Day": a doubled underscore between words stands for "' ".
    return _clean(_APOSTROPHE.sub("' ", text))


def parse_document_name(name: str) -> DocumentHint | None:
    """Parse names like "2026-11-01_All_Saints__Day_-_DS1-Eric.docx".

    Layout: `<YYYY-MM-DD>_<occasion>[_-_<setting>[-<preacher>]]`, underscores standing
    for spaces; "__" between two words of the occasion stands for an apostrophe and a
    space ("All Saints' Day"). A lone part after the separator is a setting if it
    contains a digit, otherwise a preacher. Returns None if the name does not start
    with a valid date.
    """
    stem = _EXTENSION.sub("", name.strip().rsplit("/", 1)[-1])
    match = _NAME.match(stem)
    if match is None:
        return None
    try:
        when = date.fromisoformat(match.group("date"))
    except ValueError:
        return None
    rest = match.group("rest") or ""
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
        service_date=when,
        occasion=_clean_occasion(occasion_part),
        setting=setting,
        preacher=preacher,
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
    preacher_from_documents: bool = True
    """Use the name at the end of document names as the preacher. Some churches put
    someone else there (e.g. whoever publishes the video); set false to ignore it."""
    service_time: str | None = None
    """Local "HH:MM", used when neither the defaults nor a rule provide a time."""
    title: str | None = None
    """Template such as "Sermon for {occasion}"; fields: {occasion}, {preacher}, {date}
    (ISO). Applied only when every referenced field is known and no title is set. A
    leading "The " in the occasion becomes "the " in the title."""

    occasion_from_document_text: bool = True
    """Prefer the occasion a bulletin's text starts with over the one in its name."""
    scripture: ScriptureOptions | None = None
    """Detect the sermon's Scripture (opt-in); see `georg...metadata.scripture`."""

    @field_validator("service_time")
    @classmethod
    def _service_time(cls, value: str | None) -> str | None:
        if value is not None and not _HH_MM.fullmatch(value):
            raise ValueError("service_time must be HH:MM (24-hour)")
        return value

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is not None:
            unknown = template_fields(value) - TITLE_FIELDS
            if unknown:
                raise ValueError(
                    f"title template has unknown fields {sorted(unknown)}; "
                    f"use {sorted(TITLE_FIELDS)}"
                )
        return value


def template_fields(template: str) -> set[str]:
    """Field names referenced by a `str.format` template; raises ValueError if malformed."""
    fields: set[str] = set()
    for _, name, spec, conversion in string.Formatter().parse(template):
        if name is None:
            continue
        if spec or conversion:
            raise ValueError("title template fields cannot have format specs or conversions")
        fields.add(name)
    return fields


def _title_occasion(occasion: str | None) -> str | None:
    """Lowercase a leading article for use mid-title: "Sermon for the Holy Trinity"."""
    if occasion and occasion.startswith("The "):
        return "the " + occasion[4:]
    return occasion


class RulesMetadataGenerator:
    """Module type `rules`."""

    version = "rules-3"

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        model_factory: Callable[[LlamaModelOptions], LlamaLike] | None = None,
        calendar: Calendar | None = None,
    ) -> None:
        self._calendar = calendar
        try:
            self.options = MetadataOptions.model_validate(dict(options or {}))
        except ValidationError as exc:
            raise ConfigError(f"metadata options: {exc}") from exc
        self._preachers = {k.casefold(): v for k, v in self.options.preachers.items()}
        self._scripture = (
            ScriptureDetector(self.options.scripture, model_factory=model_factory)
            if self.options.scripture is not None
            else None
        )
        if self._scripture is not None:
            self.version = f"{type(self).version}+{self._scripture.version}"
        if calendar is not None:
            self.version = f"{self.version}+{calendar.version}"
        self.uses_documents = self.options.occasion_from_document_text or bool(self._scripture)

    def generate(self, ctx: MetadataContext) -> SermonMetadata:
        return self.propose(ctx).metadata

    def release(self) -> None:
        """Free the Scripture detector's local model, if it loaded one."""
        if self._scripture is not None:
            self._scripture.release()

    def propose(self, ctx: MetadataContext) -> MetadataProposal:
        origins: dict[str, str] = {}
        hint, hint_name = self._document_hint(ctx)
        documents = _ordered_documents(ctx.documents, hint_name)
        meta = self._from_source(ctx, hint)
        meta = self._occasion(meta, hint, hint_name, documents, origins)
        meta = self._default_service_time(meta)
        meta = self._detect_scripture(meta, ctx, documents, origins)
        meta = self._default_title(meta)
        return MetadataProposal(metadata=meta, origins=origins)

    def _document_hint(self, ctx: MetadataContext) -> tuple[DocumentHint | None, str | None]:
        service_date = ctx.source.service_date
        for name in sorted(_document_names(ctx.source.hints)):
            parsed = parse_document_name(name)
            if parsed and (service_date is None or parsed.service_date == service_date):
                return parsed, name
        return None, None

    def _from_source(self, ctx: MetadataContext, hint: DocumentHint | None) -> SermonMetadata:
        service_date = ctx.source.service_date or (hint.service_date if hint else None)
        update: dict[str, Any] = {
            "service_date": service_date or ctx.defaults.service_date,
            "extra": dict(ctx.defaults.extra),
        }
        if hint is not None and hint.preacher and self.options.preacher_from_documents:
            update["preacher"] = self._preachers.get(hint.preacher.casefold(), hint.preacher)
        return ctx.defaults.model_copy(update=update)

    def _occasion(
        self,
        meta: SermonMetadata,
        hint: DocumentHint | None,
        hint_name: str | None,
        documents: Mapping[str, str],
        origins: dict[str, str],
    ) -> SermonMetadata:
        if self.options.occasion_from_document_text:
            headings = self.options.scripture.headings if self.options.scripture else None
            for name, text in documents.items():
                header = parse_header(text, headings)
                if header is None:
                    continue
                if meta.service_date is not None and header.service_date != meta.service_date:
                    log.warning(
                        "document %r is dated %s, not %s; its occasion is ignored",
                        name,
                        header.service_date,
                        meta.service_date,
                    )
                    continue
                return self._set_occasion(meta, header.occasion, f"document text: {name}", origins)
        if hint is not None and hint.occasion:
            return self._set_occasion(meta, hint.occasion, f"document name: {hint_name}", origins)
        return meta

    def _set_occasion(
        self, meta: SermonMetadata, occasion: str, source: str, origins: dict[str, str]
    ) -> SermonMetadata:
        origin = f"{type(self).version}/{source}"
        if self._calendar is not None:
            normalized = self._calendar.normalize(occasion)
            occasion = normalized.name
            note = normalized.note()
            if note:
                origin += f"; {note}"
        origins["occasion"] = origin
        return meta.model_copy(update={"occasion": occasion})

    def _detect_scripture(
        self,
        meta: SermonMetadata,
        ctx: MetadataContext,
        documents: Mapping[str, str],
        origins: dict[str, str],
    ) -> SermonMetadata:
        if self._scripture is None or meta.scripture:
            return meta
        transcript = ctx.transcript.text if ctx.transcript is not None else ""
        try:
            found = self._scripture.detect(transcript, documents)
        except Exception as exc:
            log.warning("scripture detection failed: %s", exc)
            return meta
        if found is None:
            return meta
        origins["scripture"] = found.origin
        return meta.model_copy(update={"scripture": found.references})

    def _default_service_time(self, meta: SermonMetadata) -> SermonMetadata:
        if meta.service_time or self.options.service_time is None:
            return meta
        return meta.model_copy(update={"service_time": self.options.service_time})

    def _default_title(self, meta: SermonMetadata) -> SermonMetadata:
        template = self.options.title
        if meta.title or template is None:
            return meta
        values = {
            "occasion": _title_occasion(meta.occasion),
            "preacher": meta.preacher,
            "date": meta.service_date.isoformat() if meta.service_date else None,
        }
        if any(not values[name] for name in template_fields(template)):
            return meta
        return meta.model_copy(update={"title": template.format(**values).strip() or None})


def _ordered_documents(documents: Mapping[str, str], first: str | None) -> dict[str, str]:
    """Document texts with the dated companion document first, then by name."""
    names = sorted(documents, key=lambda name: (name != first, name))
    return {name: documents[name] for name in names}
