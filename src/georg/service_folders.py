# SPDX-License-Identifier: AGPL-3.0-only
"""Service-folder layout shared by folder-based inputs and archive outputs.

A deployment keeps one subfolder per service (named like "SPX 11.01.26") under a
root folder and may later archive it into a year folder ("Services 2026") that
also lives under the root. `ServiceFolderLayout` knows that naming and how to
find or create a service's folder; a storage backend only implements the small
`FolderStore` interface, so a future Dropbox or other target reuses the layout.
"""

import fnmatch
import logging
import re
import string
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

log = logging.getLogger(__name__)

TEMPLATE_FIELDS = frozenset({"date", "mmddyy", "yyyy", "yy", "mm", "dd"})


def template_values(service_date: date) -> dict[str, str]:
    """{date} is YYYY-MM-DD and {mmddyy} is MM.DD.YY (the "SPX 11.01.26" style)."""
    return {
        "date": service_date.isoformat(),
        "mmddyy": service_date.strftime("%m.%d.%y"),
        "yyyy": service_date.strftime("%Y"),
        "yy": service_date.strftime("%y"),
        "mm": service_date.strftime("%m"),
        "dd": service_date.strftime("%d"),
    }


def check_template(value: str) -> str:
    """A folder/file name template: non-empty, no '/', known fields only."""
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
    except ValueError as exc:
        raise ValueError(f"invalid template: {exc}") from exc
    if not fields <= TEMPLATE_FIELDS:
        raise ValueError(f"unknown template fields {sorted(fields - TEMPLATE_FIELDS)}")
    if not value.strip() or "/" in value:
        raise ValueError("must be a non-empty name without '/'")
    return value


def check_date_format(value: str) -> str:
    sample = date(2026, 11, 1)
    try:
        parsed = datetime.strptime(sample.strftime(value), value).date()
    except ValueError as exc:
        raise ValueError(f"invalid date_format: {exc}") from exc
    if parsed != sample:
        raise ValueError("date_format must include the day, month, and year")
    return value


def check_patterns(value: tuple[str, ...]) -> tuple[str, ...]:
    if any(not pattern.strip() for pattern in value):
        raise ValueError("patterns must be non-empty")
    return value


def pattern_prefix(pattern: str) -> str:
    """The pattern's literal part before its first wildcard."""
    match = re.search(r"[*?\[]", pattern)
    return pattern[: match.start()] if match else pattern


def folder_date(name: str, prefix: str, date_format: str) -> date | None:
    """Parse the service date after `prefix`, ignoring any trailing words."""
    rest = name[len(prefix) :].strip() if name.startswith(prefix) else name.strip()
    candidates = [rest, *rest.split()[:1]]
    for text in candidates:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Folder:
    """A folder in a storage backend; `raw` is the backend's own record, if any."""

    ref: str
    name: str
    raw: Any = None


class FolderStore(Protocol):
    """The folder operations a storage backend must provide."""

    def list_folders(self, parent: str) -> Iterable[Folder]:
        """Immediate subfolders of `parent`, not deleted, oldest first."""
        ...

    def create_folder(self, name: str, parent: str) -> str:
        """Create a folder under `parent` and return its ref."""
        ...


@dataclass(frozen=True)
class ServiceFolder:
    """A folder holding one service's files, found by `ServiceFolderLayout`."""

    date: date
    folder: Folder
    year: Folder | None
    """The year folder it lives in, or None when it sits at the root."""


class ServiceFolderLayout(BaseModel):
    """Naming rules for service folders and the year folders they archive into."""

    model_config = ConfigDict(frozen=True)

    folder_pattern: str = "SPX *"
    """Shell-style pattern for service folder names."""
    date_format: str = "%m.%d.%y"
    """`strptime` format of the date after the pattern's literal prefix."""
    year_patterns: tuple[str, ...] = ("Services *",)
    """Patterns of top-level year folders to also look inside; empty disables it."""
    service_template: str = "SPX {mmddyy}"
    """Name template used to locate or create a service's folder."""
    year_template: str = "Services {yyyy}"
    """Name template of the year folder a service date belongs to."""
    create_in_year: bool = False
    """Create a missing service folder inside its year folder instead of at the root."""

    @field_validator("folder_pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("folder_pattern must be non-empty")
        return value

    @field_validator("year_patterns")
    @classmethod
    def _years(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_patterns(value)

    @field_validator("date_format")
    @classmethod
    def _date_format(cls, value: str) -> str:
        return check_date_format(value)

    @field_validator("service_template", "year_template")
    @classmethod
    def _template(cls, value: str) -> str:
        return check_template(value)

    @property
    def prefix(self) -> str:
        return pattern_prefix(self.folder_pattern)

    def is_year_folder(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.year_patterns)

    def service_name(self, service_date: date) -> str:
        return self.service_template.format(**template_values(service_date))

    def year_name(self, service_date: date) -> str:
        return self.year_template.format(**template_values(service_date))

    def _service(self, folder: Folder, year: Folder | None) -> ServiceFolder | None:
        service_date = folder_date(folder.name, self.prefix, self.date_format)
        if service_date is None:
            log.warning("service folder %r has no %s date; skipping", folder.name, self.date_format)
            return None
        return ServiceFolder(service_date, folder, year)

    def service_folders(self, store: FolderStore, root: str) -> Iterator[ServiceFolder]:
        """Every service folder under `root`, top-level or inside a year folder."""
        for folder in store.list_folders(root):
            if fnmatch.fnmatchcase(folder.name, self.folder_pattern):
                if found := self._service(folder, None):
                    yield found
            elif self.is_year_folder(folder.name):
                for sub in store.list_folders(folder.ref):
                    if fnmatch.fnmatchcase(sub.name, self.folder_pattern) and (
                        found := self._service(sub, folder)
                    ):
                        yield found

    def locate(self, store: FolderStore, root: str, service_date: date) -> str:
        """The ref of the service's folder wherever it is, creating it if missing."""
        name = self.service_name(service_date)
        years = []
        for folder in store.list_folders(root):
            if folder.name == name:
                return folder.ref
            if self.is_year_folder(folder.name):
                years.append(folder)
        for year in years:
            for folder in store.list_folders(year.ref):
                if folder.name == name:
                    return folder.ref
        if not self.create_in_year:
            return store.create_folder(name, root)
        year_name = self.year_name(service_date)
        year_ref = next((year.ref for year in years if year.name == year_name), None)
        if year_ref is None:
            year_ref = store.create_folder(year_name, root)
        return store.create_folder(name, year_ref)
