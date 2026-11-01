# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
from typing import BinaryIO, Protocol

from georg.core.errors import GeorgError
from georg.core.types import ArtifactRef


class ArtifactNotFoundError(GeorgError, LookupError):
    pass


class ArtifactStore(Protocol):
    """Stores immutable files keyed by their SHA-256; knows nothing of their meaning."""

    def put(self, src: Path, *, mime: str | None = None) -> ArtifactRef:
        """Store a copy of `src`. Storing identical content again is a no-op."""
        ...

    def exists(self, sha256: str) -> bool: ...

    def path(self, sha256: str) -> Path:
        """A local path to the stored file, which callers must not modify."""
        ...

    def open(self, sha256: str) -> BinaryIO: ...
