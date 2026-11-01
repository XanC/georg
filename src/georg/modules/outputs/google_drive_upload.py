# SPDX-License-Identifier: AGPL-3.0-only
"""Archive output: upload one rendition (the HQ service MP3) to a Google Drive folder.

Each upload records the publication key in the file's `appProperties`, so
`find_existing` locates it again and a retry or re-publish updates that file instead
of creating a duplicate. Content is sent with Drive's resumable upload protocol and
checked against Drive's reported size and MD5.
"""

import hashlib
import logging
import string
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from georg.core.config import OutputConfig
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import ArtifactRef, ExternalRef, PublicationRequest
from georg.gdrive import (
    FOLDER_MIME,
    SCOPE_DRIVE,
    DriveClient,
    ServiceAccountTokens,
    TokenSource,
    quote_query,
)
from georg.gdrive.client import CHUNK_ALIGN

TYPE_NAME = "google_drive_upload"
KEY_PROPERTY = "georg_publication_key"
SHA256_PROPERTY = "georg_sha256"
MAX_PROPERTY_BYTES = 124
"""Drive's limit on the UTF-8 size of an app property's key plus value."""
FILE_FIELDS = "id, name, size, md5Checksum, webViewLink, trashed"
TEMPLATE_FIELDS = frozenset({"date", "mmddyy", "yyyy", "yy", "mm", "dd"})

log = logging.getLogger(__name__)


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


class GoogleDriveUploadOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    folder_id: str = Field(min_length=1)
    """Archive folder; it should be on a shared drive (see docs/google-drive.md)."""
    credentials_file: str
    """Path to the service account JSON key, usually "env:NAME"."""
    subject: str | None = None
    """Workspace user to impersonate (domain-wide delegation); normally unset."""
    filename: str = "{date}.mp3"
    subfolder: str | None = None
    """Optional subfolder of `folder_id`, e.g. "SPX {mmddyy}"; created if missing."""
    rendition: str = "service_hq"
    mime: str = "audio/mpeg"
    chunk_size: int = Field(default=8 * 1024 * 1024, gt=0)
    """Resumable upload chunk size in bytes; a multiple of 256 KiB."""
    timeout_s: float = Field(default=120.0, gt=0)
    attempts: int = Field(default=4, ge=1)

    @field_validator("filename", "subfolder")
    @classmethod
    def _template(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            fields = {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
        except ValueError as exc:
            raise ValueError(f"invalid template: {exc}") from exc
        if not fields <= TEMPLATE_FIELDS:
            raise ValueError(f"unknown template fields {sorted(fields - TEMPLATE_FIELDS)}")
        if not value.strip() or "/" in value:
            raise ValueError("must be a non-empty name without '/'")
        return value

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
        self.renditions = frozenset({opts.rendition})
        client = http_client or httpx.Client(timeout=opts.timeout_s, follow_redirects=True)
        tokens = credentials or ServiceAccountTokens(
            opts.credentials_file, [SCOPE_DRIVE], subject=opts.subject, http_client=client
        )
        self._drive = DriveClient(tokens, client, attempts=opts.attempts, sleep=sleep)

    def filename(self, service_date: date) -> str:
        return self.options.filename.format(**template_values(service_date))

    def subfolder(self, service_date: date) -> str | None:
        template = self.options.subfolder
        return template.format(**template_values(service_date)) if template else None

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
        root = self.options.folder_id
        name = self.subfolder(service_date)
        if name is None:
            return root
        query = (
            f"'{quote_query(root)}' in parents and name = '{quote_query(name)}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        for raw in self._drive.list_files(query, "id, name", order_by="createdTime"):
            return str(raw["id"])
        return str(self._drive.create_folder(name, root)["id"])

    def _artifact(self, request: PublicationRequest) -> ArtifactRef:
        try:
            return request.files[self.options.rendition]
        except KeyError:
            raise ModuleError(
                f"output {self.name!r}: request has no {self.options.rendition!r} rendition"
            ) from None
