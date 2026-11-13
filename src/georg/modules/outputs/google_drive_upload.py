# SPDX-License-Identifier: AGPL-3.0-only
"""Archive output: upload one rendition (the HQ service MP3) to a Google Drive folder.

Each upload records the publication key in the file's `appProperties`, so
`find_existing` locates it again and a retry or re-publish updates that file instead
of creating a duplicate. Content is sent with Drive's resumable upload protocol and
checked against Drive's reported size and MD5.

The per-service subfolder (`subfolder`) is located wherever it currently lives — at
the top level of `folder_id` or inside a year folder like "Services 2026" — by the
shared `georg.service_folders` layout, so a service already archived keeps one folder
instead of getting a duplicate at the top level.
"""

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from georg.core.checks import CheckResult
from georg.core.config import OutputConfig, resolve_secret
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import ArtifactRef, ExternalRef, PublicationRequest
from georg.gdrive import (
    FOLDER_MIME,
    SCOPE_DRIVE,
    DriveClient,
    GoogleTokens,
    TokenSource,
    quote_query,
)
from georg.gdrive.client import CHUNK_ALIGN
from georg.gdrive.folders import DriveFolderStore
from georg.service_folders import (
    ServiceFolderLayout,
    check_patterns,
    check_template,
    template_values,
)

TYPE_NAME = "google_drive_upload"
KEY_PROPERTY = "georg_publication_key"
SHA256_PROPERTY = "georg_sha256"
MAX_PROPERTY_BYTES = 124
"""Drive's limit on the UTF-8 size of an app property's key plus value."""
FILE_FIELDS = "id, name, size, md5Checksum, webViewLink, trashed"

log = logging.getLogger(__name__)


class GoogleDriveUploadOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    folder_id: str = Field(min_length=1)
    """Archive folder, may be "env:NAME"; for a service account, on a shared drive."""
    credentials_file: str = "env:GOOGLE_APPLICATION_CREDENTIALS"
    """Service account key or `georg drive-login` file, usually "env:NAME"."""
    subject: str | None = None
    """Workspace user a service account impersonates (domain-wide delegation)."""
    filename: str = "{date}.mp3"
    subfolder: str | None = None
    """Optional per-service subfolder of `folder_id`, e.g. "SPX {mmddyy}"."""
    year_patterns: tuple[str, ...] = ("Services *",)
    """Top-level folders under `folder_id` the service subfolder may be archived into."""
    create_in: Literal["root", "year"] = "root"
    """Where a missing service subfolder is created: `folder_id` or its year folder."""
    year_subfolder: str = "Services {yyyy}"
    """Year folder that `create_in = "year"` creates or reuses for the service date."""
    rendition: str = "service_hq"
    mime: str = "audio/mpeg"
    chunk_size: int = Field(default=8 * 1024 * 1024, gt=0)
    """Resumable upload chunk size in bytes; a multiple of 256 KiB."""
    timeout_s: float = Field(default=120.0, gt=0)
    attempts: int = Field(default=4, ge=1)

    @field_validator("filename", "subfolder", "year_subfolder")
    @classmethod
    def _template(cls, value: str | None) -> str | None:
        return check_template(value) if value is not None else value

    @field_validator("year_patterns")
    @classmethod
    def _year_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_patterns(value)

    def layout(self) -> ServiceFolderLayout:
        return ServiceFolderLayout(
            service_template=self.subfolder or ServiceFolderLayout().service_template,
            year_patterns=self.year_patterns,
            year_template=self.year_subfolder,
            create_in_year=self.create_in == "year",
        )

    @field_validator("chunk_size")
    @classmethod
    def _aligned(cls, value: int) -> int:
        if value % CHUNK_ALIGN:
            raise ValueError("chunk_size must be a multiple of 262144 (256 KiB)")
        return value


def _hashes(path: Path) -> tuple[int, str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            md5.update(block)
            sha256.update(block)
            size += len(block)
    return size, md5.hexdigest(), sha256.hexdigest()


def _same_content(raw: dict[str, Any], size: int, md5: str) -> bool:
    return str(raw.get("size")) == str(size) and str(raw.get("md5Checksum", "")).lower() == md5


class GoogleDriveUploadOutput:
    """`OutputTarget` of type "google_drive_upload"."""

    wants_transcript = False

    def __init__(
        self,
        config: OutputConfig,
        *,
        credentials: TokenSource | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self.options = GoogleDriveUploadOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ConfigError(f"output {config.name!r}: {exc}") from exc
        opts = self.options
        if config.renditions and set(config.renditions) != {opts.rendition}:
            raise ConfigError(
                f"output {config.name!r} uploads rendition {opts.rendition!r} "
                f"but lists {list(config.renditions)}"
            )
        if config.transcript:
            raise ConfigError(f"output {config.name!r}: Drive upload cannot publish transcripts")
        self.name = config.name
        self.folder_id = resolve_secret(opts.folder_id)
        self.renditions = frozenset({opts.rendition})
        client = http_client or httpx.Client(timeout=opts.timeout_s, follow_redirects=True)
        tokens = credentials or GoogleTokens(
            opts.credentials_file, [SCOPE_DRIVE], subject=opts.subject, http_client=client
        )
        self._tokens = tokens
        self._drive = DriveClient(tokens, client, attempts=opts.attempts, sleep=sleep)
        self._store = DriveFolderStore(self._drive)
        self._layout = opts.layout()

    def check(self) -> CheckResult:
        """Refresh the token and read the archive folder's metadata; uploads nothing."""
        try:
            self._tokens.token(force_refresh=True)
        except (ModuleError, ConfigError, httpx.HTTPError, OSError, ValueError) as exc:
            return CheckResult(False, f"Token refresh failed: {exc}")
        try:
            folder = self._drive.get_file(self.folder_id, "id,mimeType,capabilities")
        except (ModuleError, httpx.HTTPError) as exc:
            return CheckResult(False, f"Token refreshed, but reading the folder failed: {exc}")
        if folder is None or folder.get("mimeType") != FOLDER_MIME:
            return CheckResult(
                False,
                "Token refreshed, but the folder was not found, is not a folder, "
                "or is not shared with this account.",
            )
        capabilities = folder.get("capabilities") or {}
        if not capabilities.get("canAddChildren"):
            return CheckResult(False, "Folder reachable, but this account can't add files to it.")
        return CheckResult(True, "Folder reachable; this account can add files to it.")

    def filename(self, service_date: date) -> str:
        return self.options.filename.format(**template_values(service_date))

    def _ref(self, raw: dict[str, Any]) -> ExternalRef:
        file_id = str(raw["id"])
        url = raw.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        return ExternalRef(target=self.name, external_id=file_id, url=url)

    def _find(self, publication_key: str) -> dict[str, Any] | None:
        query = (
            f"appProperties has {{ key='{KEY_PROPERTY}' and "
            f"value='{quote_query(publication_key)}' }} and trashed = false"
        )
        found = list(
            self._drive.list_files(query, FILE_FIELDS, corpora="allDrives", order_by="createdTime")
        )
        if len(found) > 1:
            log.warning(
                "google_drive_upload: %d files carry publication key %r; using the oldest",
                len(found),
                publication_key,
            )
        return found[0] if found else None

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        raw = self._find(publication_key)
        return self._ref(raw) if raw else None

    def publish(self, request: PublicationRequest) -> ExternalRef:
        artifact = self._artifact(request)
        key = request.publication_key
        if len(KEY_PROPERTY.encode()) + len(key.encode()) > MAX_PROPERTY_BYTES:
            raise ModuleError(f"output {self.name!r}: publication key {key!r} is too long")
        try:
            size, md5, sha256 = _hashes(artifact.path)
        except OSError as exc:
            raise ModuleError(f"output {self.name!r}: cannot read {artifact.path}: {exc}") from exc
        if size != artifact.size or sha256 != artifact.sha256:
            raise ModuleError(f"output {self.name!r}: {artifact.path} does not match its artifact")
        name = self.filename(request.service_date)
        current = self._current(request)
        if current is not None and _same_content(current, size, md5) and current["name"] == name:
            return self._ref(current)
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": self.options.mime,
            "appProperties": {KEY_PROPERTY: key, SHA256_PROPERTY: sha256},
        }
        if current is None:
            metadata["parents"] = [self._parent(request.service_date)]
        result = self._drive.upload(
            artifact.path,
            metadata,
            mime=self.options.mime,
            fields=FILE_FIELDS,
            file_id=str(current["id"]) if current is not None else None,
            chunk_size=self.options.chunk_size,
        )
        if "id" not in result or not _same_content(result, size, md5):
            raise ModuleError(
                f"output {self.name!r}: Drive reports size {result.get('size')} and md5 "
                f"{result.get('md5Checksum')} for {name}, expected {size} and {md5}",
                retryable=True,
            )
        return self._ref(result)

    def _current(self, request: PublicationRequest) -> dict[str, Any] | None:
        existing = request.existing
        if existing is not None and existing.target == self.name:
            raw = self._drive.get_file(existing.external_id, FILE_FIELDS)
            if raw is not None and not raw.get("trashed"):
                return raw
        return self._find(request.publication_key)

    def _parent(self, service_date: date) -> str:
        if self.options.subfolder is None:
            return self.folder_id
        return self._layout.locate(self._store, self.folder_id, service_date)

    def _artifact(self, request: PublicationRequest) -> ArtifactRef:
        try:
            return request.files[self.options.rendition]
        except KeyError:
            raise ModuleError(
                f"output {self.name!r}: request has no {self.options.rendition!r} rendition"
            ) from None
