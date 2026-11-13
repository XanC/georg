# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical day names (occasions) of a church calendar, with aliases.

A calendar file maps canonical names to aliases in TOML tables (the format
contrib/wp-authority reads too): `[canonical]` applies to every lectionary, and
`[three-year]` / `[one-year]` are added for the configured one. Georg bundles
`georg/data/calendars/<book>.toml`; a local file in the same format is merged over it,
and its aliases win over the bundled ones.
"""

import hashlib
import logging
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from georg.core.errors import ConfigError
from georg.modules.processors.metadata.names import clean_name, series_key, spell_ordinals
from georg.modules.processors.metadata.references import DASHES

Book = Literal["lsb"]
Lectionary = Literal["three-year", "one-year"]

LOOKUP_REVISION = "2"
"""Part of `Calendar.version`; bump when matching changes so cached metadata is redone."""

log = logging.getLogger(__name__)

_PUNCTUATION = re.compile(f"[.,'\u2019_{re.escape(DASHES)}]")


def punctuation_key(name: str) -> str:
    """Equal for names that differ only in case, `.`, `,`, apostrophes, `_`, dashes or
    spacing: "St__Luke_Evangelist" and "St. Luke, Evangelist" share a key."""
    return " ".join(_PUNCTUATION.sub(" ", clean_name(name).casefold()).split())


@dataclass(frozen=True)
class Normalized:
    name: str
    """The occasion to use: canonical if an alias matched exactly, by case or by
    punctuation and spacing, else as given."""
    known: bool
    original: str | None = None
    """The given name, when it was replaced by its canonical form."""
    suggestion: str | None = None
    """A canonical name with the same comparison key, for an unknown name."""

    def note(self) -> str | None:
        """Provenance for the review page."""
        if self.original is not None:
            return f"normalized from '{self.original}'"
        if self.known:
            return None
        hint = f"; did you mean '{self.suggestion}'?" if self.suggestion else ""
        return f"not a known day name{hint}"


def _tables(data: dict[str, object], lectionary: str, source: str) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for table in ("canonical", lectionary):
        entries = data.get(table, {})
        if not isinstance(entries, dict):
            raise ConfigError(f"{source}: [{table}] must be a table")
        for name, aliases in entries.items():
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise ConfigError(f"{source}: {name!r} must map to a list of aliases")
            merged.setdefault(name, []).extend(aliases)
    seen: dict[str, str] = {}
    for name, aliases in merged.items():
        for alias in (name, *aliases):
            key = clean_name(alias).casefold()
            if seen.setdefault(key, name) != name:
                raise ConfigError(f"{source}: {alias!r} is listed under {seen[key]!r} and {name!r}")
    return merged


class Calendar:
    def __init__(self, *layers: dict[str, list[str]], version: str = "custom") -> None:
        self.version = version
        """Changes whenever the names change, so cached metadata is redone."""
        self._lookup: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self.canonical: list[str] = []
        for layer in layers:
            for canonical, aliases in layer.items():
                canonical = clean_name(canonical)
                if canonical not in self.canonical:
                    self.canonical.append(canonical)
                for name in (canonical, *aliases):
                    for form in {clean_name(name), spell_ordinals(clean_name(name))}:
                        self._lookup[form.casefold()] = canonical
        for canonical in self.canonical:
            self._keys.setdefault(series_key(canonical), canonical)
        self._loose: dict[str, str | None] = {}
        """Punctuation key -> canonical name, or None when names of two canonical
        names share the key (then it is only a suggestion)."""
        for form, canonical in self._lookup.items():
            key = punctuation_key(form)
            if self._loose.setdefault(key, canonical) not in (canonical, None):
                log.warning(
                    "calendar: %r and %r differ only in punctuation; not normalized automatically",
                    self._loose[key],
                    canonical,
                )
                self._loose[key] = None

    def normalize(self, name: str) -> Normalized:
        cleaned = " ".join(clean_name(name).replace("\u2019", "'").split())
        spelled = spell_ordinals(cleaned)
        canonical = (
            self._lookup.get(cleaned.casefold())
            or self._lookup.get(spelled.casefold())
            or self._loose.get(punctuation_key(cleaned))
            or self._loose.get(punctuation_key(spelled))
        )
        if canonical is not None:
            return Normalized(canonical, True, cleaned if canonical != cleaned else None)
        return Normalized(cleaned, False, suggestion=self._keys.get(series_key(cleaned)))


def load_calendar(
    book: Book = "lsb", lectionary: Lectionary = "three-year", occasions: Path | None = None
) -> Calendar:
    text = resources.files("georg").joinpath("data", "calendars", f"{book}.toml").read_text("utf-8")
    layers = [_tables(tomllib.loads(text), lectionary, f"{book}.toml")]
    digest = hashlib.sha256(text.encode())
    if occasions is not None:
        try:
            local_text = occasions.read_text(encoding="utf-8")
            local = tomllib.loads(local_text)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"calendar occasions file {occasions}: {exc}") from exc
        layers.append(_tables(local, lectionary, str(occasions)))
        digest.update(local_text.encode())
    return Calendar(
        *layers, version=f"{book}-{lectionary}-{LOOKUP_REVISION}-{digest.hexdigest()[:8]}"
    )
