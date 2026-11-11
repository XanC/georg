# SPDX-License-Identifier: AGPL-3.0-only
"""The review form: rendering a revision as form values and parsing edits back."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from pydantic import ValidationError

from georg.core.types import SermonMetadata, ServiceBoundaries

BOUNDARY_FIELDS: Final = (
    ("service_start", "Service start"),
    ("sermon_start", "Sermon start"),
    ("sermon_end", "Sermon end"),
    ("service_end", "Service end"),
)
TEXT_FIELDS: Final = ("title", "preacher", "series", "occasion")
METADATA_FIELDS: Final = (
    "title",
    "preacher",
    "service_date",
    "service_time",
    "scripture",
    "series",
    "occasion",
    "summary",
)
BOUNDARIES_ORIGIN: Final = "boundaries"
TRANSCRIPT_ORIGIN: Final = "transcript"
_HMS = re.compile(r"^(?:(?:(\d+):)?([0-5]?\d):)?([0-5]?\d(?:\.\d{1,3})?)$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def format_hms(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def format_tenths(ms: int) -> str:
    """H:MM:SS.s, truncated to tenths of a second."""
    return f"{format_hms(ms)}.{ms // 100 % 10}"


def parse_hms(value: str) -> int:
    """Parse "H:MM:SS", "MM:SS" or "SS" (seconds may have a fraction) into milliseconds."""
    match = _HMS.match(value.strip())
    if not match:
        raise ValueError(f"{value!r} is not a time like 1:02:03")
    hours, minutes, seconds = match.groups()
    return round((int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)) * 1000)


def _boundary_ms(b: ServiceBoundaries) -> dict[str, int]:
    return {
        "service_start": b.service_start_ms,
        "sermon_start": b.sermon_start_ms,
        "sermon_end": b.sermon_end_ms,
        "service_end": b.service_end_ms,
    }


def form_values(
    boundaries: ServiceBoundaries, metadata: SermonMetadata, transcript: str | None
) -> dict[str, str]:
    values = {name: format_hms(ms) for name, ms in _boundary_ms(boundaries).items()}
    values.update({name: getattr(metadata, name) or "" for name in TEXT_FIELDS})
    values["service_date"] = metadata.service_date.isoformat() if metadata.service_date else ""
    values["service_time"] = metadata.service_time or ""
    values["scripture"] = "\n".join(metadata.scripture)
    values["summary"] = metadata.summary or ""
    values["transcript"] = transcript or ""
    return values


@dataclass
class ReviewEdit:
    boundaries: ServiceBoundaries
    metadata: SermonMetadata
    transcript: str | None
    changed: list[str] = field(default_factory=list)
    """Origin keys of the edited fields: "boundaries", "transcript", or a metadata field."""

    @property
    def boundaries_changed(self) -> bool:
        return BOUNDARIES_ORIGIN in self.changed


class FormError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _text(form: Mapping[str, str], name: str) -> str:
    return form.get(name, "").replace("\r\n", "\n").strip()


def parse_edit(
    form: Mapping[str, str],
    boundaries: ServiceBoundaries,
    metadata: SermonMetadata,
    transcript: str | None,
    hidden: Collection[str] = (),
) -> ReviewEdit:
    """Apply submitted form values to a revision's content.

    A boundary whose text is unchanged keeps its exact millisecond value, since the
    form shows whole seconds. `hidden` metadata fields are not on the form and keep
    their current values.
    """
    errors: list[str] = []
    current_ms = _boundary_ms(boundaries)
    new_ms: dict[str, int] = {}
    for name, label in BOUNDARY_FIELDS:
        text = form.get(name, "").strip()
        if text == format_hms(current_ms[name]):
            new_ms[name] = current_ms[name]
            continue
        try:
            new_ms[name] = parse_hms(text)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    new_boundaries = boundaries
    if not errors:
        try:
            new_boundaries = ServiceBoundaries(**{f"{k}_ms": v for k, v in new_ms.items()})
        except ValidationError:
            errors.append(
                "Times must be in order: service start ≤ sermon start < sermon end ≤ service end."
            )

    updates: dict[str, object] = {name: _text(form, name) or None for name in TEXT_FIELDS}
    updates["summary"] = _text(form, "summary") or None
    raw_date = _text(form, "service_date")
    try:
        updates["service_date"] = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        errors.append(f"Service date: {raw_date!r} is not a date like 2026-11-01")
    raw_time = _text(form, "service_time")
    if raw_time and "service_time" not in hidden and not _HHMM.match(raw_time):
        errors.append(f"Service time: {raw_time!r} is not a time like 10:30")
    updates["service_time"] = raw_time or None
    updates["scripture"] = tuple(
        line.strip() for line in _text(form, "scripture").splitlines() if line.strip()
    )
    if errors:
        raise FormError(errors)
    updates.update({name: getattr(metadata, name) for name in hidden})

    new_metadata = metadata.model_copy(update=updates)
    new_transcript: str | None = form.get("transcript", "").replace("\r\n", "\n")
    if (new_transcript or "").strip() == (transcript or "").strip():
        new_transcript = transcript
    elif not (new_transcript or "").strip():
        new_transcript = None

    changed = [BOUNDARIES_ORIGIN] if new_boundaries != boundaries else []
    changed += [n for n in METADATA_FIELDS if getattr(new_metadata, n) != getattr(metadata, n)]
    if new_transcript != transcript:
        changed.append(TRANSCRIPT_ORIGIN)
    return ReviewEdit(new_boundaries, new_metadata, new_transcript, changed)
