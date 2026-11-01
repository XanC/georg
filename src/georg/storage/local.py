# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO

from georg.core.types import ArtifactRef
from georg.storage.base import ArtifactNotFoundError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20
DEFAULT_MIME = "application/octet-stream"


class LocalStore:
    """Artifacts on the local filesystem at `<root>/<sha256[:2]>/<sha256>`.

    Files are written to a temporary file under `<root>/.tmp`, fsynced, made
    read-only, and renamed into place, so a reader never sees a partial file.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "LocalStore":
        return cls(data_dir / "artifacts")

    def location(self, sha256: str) -> Path:
        if not _SHA256.match(sha256):
            raise ValueError(f"not a lowercase hex SHA-256: {sha256!r}")
        return self.root / sha256[:2] / sha256

    def exists(self, sha256: str) -> bool:
        return self.location(sha256).is_file()

    def path(self, sha256: str) -> Path:
        location = self.location(sha256)
        if not location.is_file():
            raise ArtifactNotFoundError(f"artifact {sha256} not in store")
        return location

    def open(self, sha256: str) -> BinaryIO:
        return self.path(sha256).open("rb")

    def put(self, src: Path, *, mime: str | None = None) -> ArtifactRef:
        tmp_dir = self.root / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
                while chunk := inp.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha256 = digest.hexdigest()
            final = self.location(sha256)
            if final.is_file() and final.stat().st_size == size:
                tmp.unlink()
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                tmp.chmod(0o444)
                os.replace(tmp, final)
                _fsync_dir(final.parent)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return ArtifactRef(path=final, sha256=sha256, size=size, mime=mime or _guess_mime(src))


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or DEFAULT_MIME


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
