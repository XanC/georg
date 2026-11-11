# SPDX-License-Identifier: AGPL-3.0-only
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from georg.core.errors import ModuleError
from georg.modules.inputs.local_folder import LocalFolderInput, folder_date


def touch(path: Path, age_s: float = 3600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def test_discovers_settled_folders_with_ordered_parts(tmp_path: Path) -> None:
    touch(tmp_path / "SPX 11.01.26" / "b.mp4")
    touch(tmp_path / "SPX 11.01.26" / "a.MOV")
    touch(tmp_path / "SPX 11.01.26" / "2026-11-01_All_Saints__Day_-_DS1-Eric.docx")
    touch(tmp_path / "SPX 11.08.26" / "only.docx")
    touch(tmp_path / "SPX 11.15.26" / "copying.mp4", age_s=1)
    touch(tmp_path / "Other" / "x.mp4")
    source = LocalFolderInput.from_options({"root": tmp_path, "folder_pattern": "SPX *"})
    [item] = source.discover(None)
    assert [p.name for p in item.parts] == ["a.MOV", "b.mp4"]
    assert item.service_date == date(2026, 11, 1)
    assert item.hints["documents"] == ["2026-11-01_All_Saints__Day_-_DS1-Eric.docx"]
    assert source.fetch(item, tmp_path / "unused") == [Path(p.ref) for p in item.parts]


def test_discover_filters_by_service_date_not_mtime(tmp_path: Path) -> None:
    # The older folder was copied recently; `since` still goes by the folder's date.
    touch(tmp_path / "SPX 10.25.26" / "a.mp4", age_s=0)
    touch(tmp_path / "SPX 11.01.26" / "a.mp4", age_s=0)
    touch(tmp_path / "undated" / "a.mp4", age_s=0)
    source = LocalFolderInput.from_options({"root": tmp_path, "settle_seconds": 0})
    since = datetime(2026, 11, 1, tzinfo=UTC)
    assert [i.title for i in source.discover(since)] == ["SPX 11.01.26", "undated"]
    assert {i.title for i in source.discover(None)} == {
        "SPX 10.25.26", "SPX 11.01.26", "undated"
    }  # fmt: skip


def test_fetch_fails_when_files_disappear(tmp_path: Path) -> None:
    part = touch(tmp_path / "s" / "a.mp4")
    source = LocalFolderInput.from_options({"root": tmp_path})
    [item] = source.discover(None)
    part.unlink()
    with pytest.raises(ModuleError, match="gone"):
        source.fetch(item, tmp_path)


def test_missing_root_is_retryable(tmp_path: Path) -> None:
    with pytest.raises(ModuleError) as err:
        list(LocalFolderInput.from_options({"root": tmp_path / "nope"}).discover(None))
    assert err.value.retryable


@pytest.mark.parametrize(
    ("name", "expected"),
    [("SPX 11.01.26", date(2026, 11, 1)), ("2026-07-26 service", date(2026, 7, 26)),
     ("SPX 13.40.26", None), ("misc", None)],
)  # fmt: skip
def test_folder_date(name: str, expected: date | None) -> None:
    assert folder_date(name) == expected


def test_fetch_document_uses_files_in_place(tmp_path: Path) -> None:
    touch(tmp_path / "SPX 11.01.26" / "a.mp4")
    doc = touch(tmp_path / "SPX 11.01.26" / "bulletin.docx")
    source = LocalFolderInput.from_options({"root": tmp_path, "folder_pattern": "SPX *"})
    [item] = source.discover(None)
    assert source.fetch_document(item, "bulletin.docx", tmp_path / "unused") == doc.resolve()
    assert source.fetch_document(item, "missing.docx", tmp_path) is None
    assert source.fetch_document(item, "../SPX 11.01.26/bulletin.docx", tmp_path) is None
