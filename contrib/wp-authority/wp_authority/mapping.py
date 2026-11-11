# SPDX-License-Identifier: AGPL-3.0-only
"""The editable mapping file (see mapping.example.toml)."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wp_authority.scripture import clean_name


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptureRules(_Strict):
    dash: Literal["-", "\u2013"] = "-"
    nest_under_books: bool = False
    fix: dict[str, list[str]] = Field(default_factory=dict)
    """Term name -> the references it should become ([] drops the term from sermons)."""


class Canonical(_Strict):
    canonical: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_canonical_per_alias(self) -> "Canonical":
        seen: dict[str, str] = {}
        for canonical, aliases in self.canonical.items():
            for name in (canonical, *aliases):
                key = clean_name(name).casefold()
                if seen.get(key, canonical) != canonical:
                    raise ValueError(f"{name!r} is listed under {seen[key]!r} and {canonical!r}")
                seen[key] = canonical
        return self

    def aliases(self) -> dict[str, str]:
        """Clean, casefolded alias or canonical name -> canonical name."""
        out = {}
        for canonical, aliases in self.canonical.items():
            for name in (canonical, *aliases):
                out[clean_name(name).casefold()] = clean_name(canonical)
        return out

    def resolve(self, name: str) -> str:
        cleaned = clean_name(name)
        return self.aliases().get(cleaned.casefold(), cleaned)


class SeriesRules(Canonical):
    assign: dict[str, str] = Field(default_factory=dict)
    """Sermon post ID -> series, e.g. copied from the plan's proposed fills."""


class Person(_Strict):
    terms: list[str]
    description: str | None = None


class SpeakerRules(Canonical):
    people: dict[str, list[str] | Person] = Field(default_factory=dict)
    """Person -> speaker terms to nest under a parent term named for the person, or
    {terms = [...], description = "..."} to also set the parent's description."""

    def persons(self) -> dict[str, Person]:
        return {
            name: entry if isinstance(entry, Person) else Person(terms=entry)
            for name, entry in self.people.items()
        }


class UnusedRules(_Strict):
    delete: bool = True


class Mapping(_Strict):
    scripture: ScriptureRules = Field(default_factory=ScriptureRules)
    series: SeriesRules = Field(default_factory=SeriesRules)
    speakers: SpeakerRules = Field(default_factory=SpeakerRules)
    unused: UnusedRules = Field(default_factory=UnusedRules)

    @classmethod
    def load(cls, path: Path | None) -> "Mapping":
        if path is None:
            return cls()
        with path.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))
