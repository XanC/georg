# SPDX-License-Identifier: AGPL-3.0-only
"""A directory whose subfolders are services, e.g. a synced Drive or a test corpus.

Each subfolder matching `folder_pattern` is one recording. Its media files, in name
order, are the parts; its other files are companion documents (metadata hints).
"""

import mimetypes
import re
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from georg.core.errors import ModuleError
from georg.core.types import SourceItem, SourcePart

MEDIA_EXTENSIONS = (".mp4", ".mkv", ".mov", ".m4v", ".mp3", ".m4a", ".wav", ".flac")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.(\d{2})(?!\d)")


class LocalFolderOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    folder_pattern: str = "*"
    settle_seconds: float = Field(default=60.0, ge=0)
    """Skip folders with files modified this recently (still being copied)."""
    media_extensions: tuple[str, ...] = MEDIA_EXTENSIONS


def folder_date(name: str) -> date | None:
    """The first `YYYY-MM-DD` or `MM.DD.YY` date in a folder name ("SPX 11.01.26")."""
    match = _DATE.search(name)
    if not match:
        return None
    try:
        if match[1]:
            return date(int(match[1]), int(match[2]), int(match[3]))
        return date(2000 + int(match[6]), int(match[4]), int(match[5]))
    except ValueError:
        return None


class LocalFolderInput:
    source_type = "local_folder"

    def __init__(self, options: LocalFolderOptions) -> None:
        self.options = options

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "LocalFolderInput":
        return cls(LocalFolderOptions.model_validate(options))

    def discover(self, since: datetime | None) -> Iterable[SourceItem]:
        root = self.options.root
        if not root.is_dir():
            raise ModuleError(f"input folder {root} does not exist", retryable=True)
        settled_before = time.time() - self.options.settle_seconds
        for folder in sorted(root.glob(self.options.folder_pattern)):
            if not folder.is_dir():
                continue
            files = sorted(
                f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")
            )
            media = [f for f in files if f.suffix.lower() in self.options.media_extensions]
            if not media:
                continue
            stats = {f: f.stat() for f in files}
            newest = max(st.st_mtime for st in stats.values())
            if newest > settled_before:
                continue
            service_date = folder_date(folder.name)
            if since is not None and service_date is not None and service_date < since.date():
                continue
            yield SourceItem(
                source_type=self.source_type,
                source_ref=str(folder.resolve()),
                parts=tuple(
                    SourcePart(
                        ref=str(f.resolve()),
                        name=f.name,
                        size=stats[f].st_size,
                        modified_at=datetime.fromtimestamp(stats[f].st_mtime, UTC),
                        mime=mimetypes.guess_type(f.name)[0],
                    )
                    for f in media
                ),
                title=folder.name,
                service_date=service_date,
                hints={"documents": [f.name for f in files if f not in media]},
            )

    def fetch(self, item: SourceItem, dest_dir: Path) -> list[Path]:
        """The parts are already local, so they are used in place."""
        paths = [Path(part.ref) for part in item.parts]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise ModuleError(f"recording files are gone: {', '.join(missing)}")
        return paths
