# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import stat
from pathlib import Path

import pytest

from georg.storage import ArtifactNotFoundError, ArtifactStore, LocalStore


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore.for_data_dir(tmp_path / "data")


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_put_is_content_addressed(store: LocalStore, tmp_path: Path) -> None:
    data = b"sermon audio" * 1000
    ref = store.put(_file(tmp_path, "sermon.mp3", data))
    sha = hashlib.sha256(data).hexdigest()
    assert ref.sha256 == sha
    assert ref.size == len(data)
    assert ref.mime == "audio/mpeg"
    assert ref.path == tmp_path / "data" / "artifacts" / sha[:2] / sha
    assert store.path(sha) == ref.path
    assert ref.path.read_bytes() == data
    assert not ref.path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    with store.open(sha) as f:
        assert f.read() == data


def test_put_is_idempotent(store: LocalStore, tmp_path: Path) -> None:
    first = store.put(_file(tmp_path, "a.json", b"{}"))
    mtime = first.path.stat().st_mtime_ns
    second = store.put(_file(tmp_path, "b.json", b"{}"), mime="application/json")
    assert second.sha256 == first.sha256
    assert second.path == first.path
    assert first.path.stat().st_mtime_ns == mtime
    assert list((store.root / ".tmp").iterdir()) == []
    assert [p for p in store.root.rglob("*") if p.is_file()] == [first.path]


def test_different_content_gets_different_keys(store: LocalStore, tmp_path: Path) -> None:
    a = store.put(_file(tmp_path, "a", b"a"))
    b = store.put(_file(tmp_path, "b", b"b"))
    assert a.sha256 != b.sha256
    assert a.mime == "application/octet-stream"


def test_missing_and_invalid_keys(store: LocalStore) -> None:
    missing = "0" * 64
    assert not store.exists(missing)
    with pytest.raises(ArtifactNotFoundError):
        store.path(missing)
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")


def test_failed_put_leaves_no_temp_file(store: LocalStore, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        store.put(tmp_path / "does-not-exist")
    assert list((store.root / ".tmp").iterdir()) == []


def test_satisfies_protocol(store: LocalStore) -> None:
    _: ArtifactStore = store
