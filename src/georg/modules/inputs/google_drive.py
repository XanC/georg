# SPDX-License-Identifier: AGPL-3.0-only
"""`InputSource` for recordings uploaded to a Google Drive folder.

The configured folder holds one subfolder per service, named like "SPX 11.01.26", either
at its top level or inside a year folder like "Services 2026" (see
`georg.service_folders`). Every video in a subfolder is one part of the recording (an
interrupted recording has several, joined in name order); other files, such as the
bulletin document, are passed on as `hints["documents"]`. A subfolder is reported only
once all of its videos have a size and have not been modified for `stable_minutes`, so
uploads in progress are skipped.

Parts are handed to the pipeline as `RemoteMedia` (`fetch_media`): ffmpeg streams each
`alt=media` URL straight from Drive (which supports HTTP range reads) and only the
extracted audio lands on disk, so a recording larger than the data volume still works.

Access is as a service account the folder is shared with, or as a Google account that
signed in with `georg drive-login` (docs/google-drive.md).
"""

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
from georg.core.types import MediaInput, RemoteMedia, SourceItem, SourcePart
from georg.gdrive import (
    FOLDER_MIME,
    SCOPE_READONLY,
    DriveClient,
    GoogleTokens,
    TokenSource,
    parse_time,
    quote_query,
)
from georg.gdrive.folders import DriveFolderStore
from georg.service_folders import (
    Folder,
    ServiceFolderLayout,
    check_date_format,
    check_patterns,
    folder_date,  # noqa: F401  (re-exported for existing imports)
    pattern_prefix,
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
    year_patterns: tuple[str, ...] = ("Services *",)
    """Top-level folders service folders may be archived into; empty disables the search."""
    video_extensions: tuple[str, ...] = ("mp4", "mkv", "mov", "m4v")
    stable_minutes: float = Field(default=10, ge=0)
    """A video counts as complete once Drive's `modifiedTime` is at least this old."""
    include_shared_drives: bool = True
    timeout_s: float = Field(default=60.0, gt=0)
    attempts: int = Field(default=4, ge=1)
    quota_retry_after_hours: float = Field(default=6, gt=0)
    """Wait before retrying when Drive reports a download quota or rate limit."""

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
        return check_date_format(value)

    @field_validator("year_patterns")
    @classmethod
    def _year_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_patterns(value)

    @property
    def prefix(self) -> str:
        """The pattern's literal part before its first wildcard."""
        return pattern_prefix(self.folder_pattern)

    def layout(self) -> ServiceFolderLayout:
        return ServiceFolderLayout(
            folder_pattern=self.folder_pattern,
            date_format=self.date_format,
            year_patterns=self.year_patterns,
        )


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
        self._store = DriveFolderStore(self._drive, all_drives=opts.include_shared_drives)
        self._layout = opts.layout()
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
        found = [
            service
            for service in self._layout.service_folders(self._store, self.folder_id)
            if since is None or service.date >= since.date()
        ]
        found.sort(key=lambda service: (service.date, service.folder.name))
        for service in found:
            item = self._item(service.folder, service.date)
            if item is not None:
                yield item

    def _item(self, folder: Folder, service_date: date) -> SourceItem | None:
        name = folder.name
        files = list(self._children(folder.ref))
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
            source_ref=folder.ref,
            parts=tuple(_part(v) for v in videos),
            title=name,
            service_date=service_date,
            recorded_at=min(created) if created else None,
            url=f"https://drive.google.com/drive/folders/{folder.ref}",
            hints={"documents": documents, "folder_name": name},
        )

    def fetch_media(self, item: SourceItem, dest_dir: Path) -> list[MediaInput]:
        """Return `RemoteMedia` for each part; ffmpeg streams them from Drive."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        media: list[MediaInput] = []
        for index, part in enumerate(item.parts, start=1):
            self._check_unchanged(part)
            media.append(
                RemoteMedia(
                    url=self._drive.media_url(part.ref),
                    name=local_name(index, part),
                    key=f"drive\0{part.ref}\0{part.size}\0{part.checksum}",
                    headers=self._drive.media_headers,
                    quota_retry_after=timedelta(hours=self.options.quota_retry_after_hours),
                    size=part.size,
                )
            )
        return media

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

    def fetch_document(self, item: SourceItem, name: str, dest_dir: Path) -> Path | None:
        """Download a companion document from the service folder into `dest_dir`.

        The local name includes the Drive file ID and md5, so an unchanged document is
        downloaded once. Google Docs/Sheets (no binary content) return None.
        """
        query = f" and name = '{quote_query(name)}'"
        found = [f for f in self._children(item.source_ref, query) if f.get("name") == name]
        if not found:
            return None
        doc = _part(found[0])
        if (doc.mime or "").startswith("application/vnd.google-apps."):
            log.info("google_drive: %r is a native Google file; not downloaded", name)
            return None
        final = dest_dir / _UNSAFE.sub("_", f"{doc.ref}-{doc.checksum}-{PurePath(name).name}")
        if doc.checksum is not None and final.exists() and self._matches(final, doc):
            return final
        dest_dir.mkdir(parents=True, exist_ok=True)
        partial = final.with_name(f".{final.name}.partial")
        try:
            size, md5 = self._drive.download(doc.ref, partial)
            self._verify(doc, size, md5)
            os.replace(partial, final)
        finally:
            partial.unlink(missing_ok=True)
        return final

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
