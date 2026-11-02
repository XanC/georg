# SPDX-License-Identifier: AGPL-3.0-only
"""`InputSource` for recordings uploaded to a Google Drive folder.

The configured folder holds one subfolder per service, named like "SPX 11.01.26". Every
video in a subfolder is one part of the recording (an interrupted recording has several,
joined in name order); other files, such as the bulletin document, are passed on as
`hints["documents"]`. A subfolder is reported only once all of its videos have a size
and have not been modified for `stable_minutes`, so uploads in progress are skipped.

Access is as a service account the folder is shared with, or as a Google account that
signed in with `georg drive-login` (docs/google-drive.md).
"""

import fnmatch
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePath
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from georg.core.config import NamedModuleConfig, resolve_secret
from georg.core.errors import ConfigError, ModuleError, SourceNotReadyError
from georg.core.types import SourceItem, SourcePart
from georg.gdrive import (
    FOLDER_MIME,
    SCOPE_READONLY,
    DriveClient,
    GoogleTokens,
    TokenSource,
    parse_time,
    quote_query,
)

TYPE_NAME = "google_drive"
FILE_FIELDS = "id, name, mimeType, size, md5Checksum, modifiedTime, createdTime, trashed"
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")

log = logging.getLogger(__name__)


class GoogleDriveInputOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    folder_id: str = Field(min_length=1)
    """The shared folder that contains one subfolder per service; may be "env:NAME"."""
    credentials_file: str = "env:GOOGLE_APPLICATION_CREDENTIALS"
    """Service account key or `georg drive-login` file, usually "env:NAME"."""
    subject: str | None = None
    """Workspace user a service account impersonates (domain-wide delegation)."""
    folder_pattern: str = "SPX *"
    """Shell-style pattern for service subfolder names."""
    date_format: str = "%m.%d.%y"
    """`strptime` format of the date that follows the pattern's literal prefix."""
    video_extensions: tuple[str, ...] = ("mp4", "mkv", "mov", "m4v")
    stable_minutes: float = Field(default=10, ge=0)
    """A video counts as complete once Drive's `modifiedTime` is at least this old."""
    include_shared_drives: bool = True
    timeout_s: float = Field(default=60.0, gt=0)
    attempts: int = Field(default=4, ge=1)

    @field_validator("video_extensions")
    @classmethod
    def _extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(e.lower().lstrip(".") for e in value if e.strip("."))
        if not cleaned:
            raise ValueError("at least one video extension is required")
        return cleaned

    @field_validator("date_format")
    @classmethod
    def _date_format(cls, value: str) -> str:
        sample = date(2026, 11, 1)
        try:
            parsed = datetime.strptime(sample.strftime(value), value).date()
        except ValueError as exc:
            raise ValueError(f"invalid date_format: {exc}") from exc
        if parsed != sample:
            raise ValueError("date_format must include the day, month, and year")
        return value

    @property
    def prefix(self) -> str:
        """The pattern's literal part before its first wildcard."""
        match = re.search(r"[*?\[]", self.folder_pattern)
        return self.folder_pattern[: match.start()] if match else self.folder_pattern


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


def _size(raw: Mapping[str, Any]) -> int | None:
    try:
        return int(raw["size"])
    except (KeyError, TypeError, ValueError):
        return None


def _part(raw: Mapping[str, Any]) -> SourcePart:
    return SourcePart(
        ref=str(raw["id"]),
        name=str(raw.get("name", "")),
        size=_size(raw),
        checksum=raw.get("md5Checksum"),
        modified_at=parse_time(raw.get("modifiedTime")),
        mime=raw.get("mimeType"),
    )


def _part_order(raw: Mapping[str, Any]) -> tuple[str, datetime, str]:
    created = parse_time(raw.get("createdTime")) or datetime.min.replace(tzinfo=UTC)
    return str(raw.get("name", "")), created, str(raw["id"])


def _md5_file(path: Path) -> tuple[int, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            md5.update(block)
            size += len(block)
    return size, md5.hexdigest()


def local_name(index: int, part: SourcePart) -> str:
    """A collision-free, filesystem-safe local name that keeps the part order visible."""
    base = _UNSAFE.sub("_", PurePath(part.name).name).strip(" .") or "part"
    return f"{index:02d}-{base}"


class GoogleDriveInput:
    """`InputSource` of type "google_drive"."""

    def __init__(
        self,
        config: NamedModuleConfig,
        *,
        credentials: TokenSource | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        try:
            self.options = GoogleDriveInputOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ConfigError(f"input {config.name!r}: {exc}") from exc
        opts = self.options
        self.name = config.name
        self.folder_id = resolve_secret(opts.folder_id)
        client = http_client or httpx.Client(timeout=opts.timeout_s, follow_redirects=True)
        tokens = credentials or GoogleTokens(
            opts.credentials_file, [SCOPE_READONLY], subject=opts.subject, http_client=client
        )
        self._drive = DriveClient(tokens, client, attempts=opts.attempts, sleep=sleep)
        self._clock = clock

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "GoogleDriveInput":
        """Build from bare options, as `georg.pipeline.registry.build_input` does."""
        return cls(NamedModuleConfig(name=TYPE_NAME, type=TYPE_NAME, options=dict(options)))

    def _children(self, folder_id: str, extra: str = "") -> Iterator[dict[str, Any]]:
        query = f"'{quote_query(folder_id)}' in parents and trashed = false{extra}"
        return self._drive.list_files(
            query, FILE_FIELDS, all_drives=self.options.include_shared_drives
        )

    def _is_video(self, raw: Mapping[str, Any]) -> bool:
        suffix = PurePath(str(raw.get("name", ""))).suffix.lower().lstrip(".")
        return raw.get("mimeType") != FOLDER_MIME and suffix in self.options.video_extensions

    def _is_stable(self, raw: Mapping[str, Any], now: datetime) -> bool:
        modified = parse_time(raw.get("modifiedTime"))
        if _size(raw) is None or modified is None:
            return False
        return now - modified >= timedelta(minutes=self.options.stable_minutes)

    def discover(self, since: datetime | None) -> Iterator[SourceItem]:
        opts = self.options
        folders = []
        for raw in self._children(self.folder_id, f" and mimeType = '{FOLDER_MIME}'"):
            name = str(raw.get("name", ""))
            if not fnmatch.fnmatchcase(name, opts.folder_pattern):
                continue
            service_date = folder_date(name, opts.prefix, opts.date_format)
            if service_date is None:
                log.warning("google_drive: skipping %r: no %s date", name, opts.date_format)
                continue
            if since is not None and service_date < since.date():
                continue
            folders.append((service_date, name, raw))
        folders.sort(key=lambda f: (f[0], f[1]))
        for service_date, name, folder in folders:
            item = self._item(folder, name, service_date)
            if item is not None:
                yield item

    def _item(self, folder: Mapping[str, Any], name: str, service_date: date) -> SourceItem | None:
        files = list(self._children(str(folder["id"])))
        videos = sorted((f for f in files if self._is_video(f)), key=_part_order)
        if not videos:
            log.info("google_drive: %r has no video yet", name)
            return None
        now = self._clock()
        unstable = [str(v.get("name")) for v in videos if not self._is_stable(v, now)]
        if unstable:
            log.info("google_drive: %r still uploading: %s", name, ", ".join(unstable))
            return None
        documents = sorted(
            str(f.get("name", ""))
            for f in files
            if f.get("mimeType") != FOLDER_MIME and not self._is_video(f)
        )
        created = [t for t in (parse_time(v.get("createdTime")) for v in videos) if t]
        return SourceItem(
            source_type=TYPE_NAME,
            source_ref=str(folder["id"]),
            parts=tuple(_part(v) for v in videos),
            title=name,
            service_date=service_date,
            recorded_at=min(created) if created else None,
            url=f"https://drive.google.com/drive/folders/{folder['id']}",
            hints={"documents": documents, "folder_name": name},
        )

    def fetch(self, item: SourceItem, dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, part in enumerate(item.parts, start=1):
            self._check_unchanged(part)
            final = dest_dir / local_name(index, part)
            if final.exists() and self._matches(final, part):
                paths.append(final)
                continue
            partial = final.with_name(f".{final.name}.partial")
            try:
                size, md5 = self._drive.download(part.ref, partial)
                self._verify(part, size, md5)
                os.replace(partial, final)
            finally:
                partial.unlink(missing_ok=True)
            paths.append(final)
        return paths

    def _check_unchanged(self, part: SourcePart) -> None:
        raw = self._drive.get_file(part.ref, FILE_FIELDS)
        if raw is None or raw.get("trashed"):
            raise SourceNotReadyError(f"google drive part {part.name!r} no longer exists")
        current = _part(raw)
        changed = [
            field
            for field in ("size", "checksum", "modified_at")
            if getattr(part, field) is not None and getattr(current, field) != getattr(part, field)
        ]
        if changed:
            raise SourceNotReadyError(
                f"google drive part {part.name!r} changed since discovery ({', '.join(changed)})"
            )

    @staticmethod
    def _matches(path: Path, part: SourcePart) -> bool:
        if part.size is None or path.stat().st_size != part.size:
            return False
        return part.checksum is None or _md5_file(path)[1] == part.checksum.lower()

    @staticmethod
    def _verify(part: SourcePart, size: int, md5: str) -> None:
        if part.size is not None and size != part.size:
            raise ModuleError(
                f"google drive part {part.name!r}: downloaded {size} bytes, expected {part.size}",
                retryable=True,
            )
        if part.checksum is not None and md5 != part.checksum.lower():
            raise ModuleError(
                f"google drive part {part.name!r}: md5 {md5} != {part.checksum}", retryable=True
            )
