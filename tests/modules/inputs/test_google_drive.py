# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fake_drive import FOLDER_MIME, FakeDrive

from georg.core.config import NamedModuleConfig
from georg.core.contracts import InputSource, MediaInputSource
from georg.core.errors import ConfigError, ModuleError, SourceNotReadyError
from georg.core.types import SourceItem
from georg.modules.inputs.google_drive import GoogleDriveInput, folder_date

NOW = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)
OLD = NOW - timedelta(hours=2)
ROOT = "root-folder"


class FakeTokens:
    def token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


def make_input(drive: FakeDrive, **options: Any) -> GoogleDriveInput:
    opts = {"folder_id": ROOT, "credentials_file": "env:GEORG_TEST_DRIVE_KEY", **options}
    config = NamedModuleConfig(name="drive", type="google_drive", options=opts)
    return GoogleDriveInput(
        config,
        credentials=FakeTokens(),
        http_client=drive.client(),
        sleep=lambda _: None,
        clock=lambda: NOW,
    )


def service_folder(drive: FakeDrive, name: str, *videos: tuple[str, bytes]) -> str:
    folder = drive.folder(name, ROOT)
    for video, content in videos:
        drive.add(video, folder.id, content, modified=OLD)
    return folder.id


def test_conforms_to_input_source(drive: FakeDrive) -> None:
    source = make_input(drive)
    assert isinstance(source, InputSource)
    assert isinstance(source, MediaInputSource)


def test_folder_date_parsing() -> None:
    assert folder_date("SPX 11.01.26", "SPX ", "%m.%d.%y") == date(2026, 11, 1)
    assert folder_date("SPX 10.25.26 (late)", "SPX ", "%m.%d.%y") == date(2026, 10, 25)
    assert folder_date("SPX 2026-11-01", "SPX ", "%Y-%m-%d") == date(2026, 11, 1)
    assert folder_date("SPX notes", "SPX ", "%m.%d.%y") is None
    assert folder_date("SPX 13.45.26", "SPX ", "%m.%d.%y") is None


def test_discover_lists_complete_services_across_pages(drive: FakeDrive) -> None:
    nov = service_folder(drive, "SPX 11.01.26", ("service.mp4", b"video"))
    drive.add("2026-11-01_All_Saints__Day_-_DS1-Eric.docx", nov, b"doc", mime="application/msword")
    drive.add("bulletin.pdf", nov, b"pdf", mime="application/pdf")
    drive.folder("extras", nov)
    service_folder(drive, "SPX 10.25.26", ("A.MOV", b"older"))
    service_folder(drive, "Choir rehearsal 11.01.26", ("x.mp4", b"x"))
    service_folder(drive, "SPX misc", ("x.mp4", b"x"))
    service_folder(drive, "SPX 11.08.26")
    drive.add("SPX 11.15.26.mp4", ROOT, b"loose file, not a folder")
    drive.page_size = 2

    items = list(make_input(drive).discover(None))

    assert [i.service_date for i in items] == [date(2026, 10, 25), date(2026, 11, 1)]
    item = items[1]
    assert item.source_type == "google_drive" and item.source_ref == nov
    assert item.title == "SPX 11.01.26"
    assert item.url == f"https://drive.google.com/drive/folders/{nov}"
    assert item.hints["documents"] == ["2026-11-01_All_Saints__Day_-_DS1-Eric.docx", "bulletin.pdf"]
    (part,) = item.parts
    assert part.name == "service.mp4" and part.size == 5 and part.mime == "video/mp4"
    assert part.checksum == hashlib.md5(b"video").hexdigest()
    assert part.modified_at == OLD
    assert items[0].parts[0].name == "A.MOV"
    list_calls = drive.calls("GET", "/drive/v3/files")
    assert any("pageToken" in str(r.url) for r in list_calls)
    assert all(r.url.params["supportsAllDrives"] == "true" for r in list_calls)


def test_discover_finds_services_archived_into_year_folders(drive: FakeDrive) -> None:
    service_folder(drive, "SPX 11.01.26", ("service.mp4", b"video"))
    archived = drive.folder("SPX 10.25.26", drive.folder("Services 2026", ROOT).id)
    drive.add("a.mp4", archived.id, b"archived", modified=OLD)
    old = drive.folder("SPX 12.28.25", drive.folder("Services 2025", ROOT).id)
    drive.add("b.mp4", old.id, b"older", modified=OLD)
    drive.folder("Misc", ROOT)
    drive.folder("Assets Audio", ROOT)
    drive.folder("Services of interest", ROOT)  # no matching service folders inside
    drive.add("SPX 11.08.26.mp4", ROOT, b"loose file, not a folder")

    items = list(make_input(drive).discover(None))

    assert [i.title for i in items] == ["SPX 12.28.25", "SPX 10.25.26", "SPX 11.01.26"]
    assert items[1].source_ref == archived.id
    assert items[0].service_date == date(2025, 12, 28)


def test_discover_year_patterns_disabled_or_custom(drive: FakeDrive) -> None:
    year = drive.folder("Services 2026", ROOT)
    archived = drive.folder("SPX 10.25.26", year.id)
    drive.add("a.mp4", archived.id, b"archived", modified=OLD)
    by_year = drive.folder("2026", ROOT)
    other = drive.folder("SPX 11.01.26", by_year.id)
    drive.add("b.mp4", other.id, b"other scheme", modified=OLD)

    assert [i.title for i in make_input(drive, year_patterns=[]).discover(None)] == []
    custom = make_input(drive, year_patterns=["Services *", "20??"])
    assert [i.title for i in custom.discover(None)] == ["SPX 10.25.26", "SPX 11.01.26"]


def test_discover_orders_parts_by_name_then_created_time(drive: FakeDrive) -> None:
    folder = drive.folder("SPX 11.01.26", ROOT)
    t = OLD - timedelta(hours=1)
    drive.add("service.mp4", folder.id, b"3", created=t + timedelta(minutes=30), modified=OLD)
    drive.add("restart.mp4", folder.id, b"2", created=t + timedelta(minutes=40), modified=OLD)
    drive.add("service.mp4", folder.id, b"1", created=t, modified=OLD, id="first")
    drive.page_size = 1
    (item,) = make_input(drive).discover(None)
    assert [p.name for p in item.parts] == ["restart.mp4", "service.mp4", "service.mp4"]
    assert item.parts[1].ref == "first"
    assert item.recorded_at == t


def test_discover_skips_folders_still_uploading(drive: FakeDrive) -> None:
    recent = service_folder(drive, "SPX 11.01.26", ("part1.mp4", b"done"))
    drive.add("part2.mp4", recent, b"growing", modified=NOW - timedelta(minutes=3))
    no_size = drive.folder("SPX 11.08.26", ROOT)
    drive.add("v.mp4", no_size.id, modified=OLD, has_size=False)
    service_folder(drive, "SPX 11.15.26", ("v.mkv", b"ok"))
    items = list(make_input(drive).discover(None))
    assert [i.title for i in items] == ["SPX 11.15.26"]
    assert [i.title for i in make_input(drive, stable_minutes=2).discover(None)] == [
        "SPX 11.01.26",
        "SPX 11.15.26",
    ]


def test_discover_since_filters_year_folders(drive: FakeDrive) -> None:
    service_folder(drive, "SPX 11.01.26", ("v.mp4", b"new"))
    archived = drive.folder("SPX 10.25.26", drive.folder("Services 2026", ROOT).id)
    drive.add("a.mp4", archived.id, b"archived", modified=OLD)
    old = drive.folder("SPX 12.28.25", drive.folder("Services 2025", ROOT).id)
    drive.add("b.mp4", old.id, b"older", modified=OLD)

    since = datetime(2026, 11, 1, tzinfo=UTC)
    assert [i.title for i in make_input(drive).discover(since)] == ["SPX 11.01.26"]


def test_discover_since_and_custom_naming(drive: FakeDrive) -> None:
    service_folder(drive, "SPX 10.25.26", ("v.mp4", b"a"))
    service_folder(drive, "SPX 11.01.26", ("v.mp4", b"b"))
    service_folder(drive, "Service 2026-11-08", ("v.webm", b"c"))
    since = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert [i.title for i in make_input(drive).discover(since)] == ["SPX 11.01.26"]
    custom = make_input(
        drive, folder_pattern="Service *", date_format="%Y-%m-%d", video_extensions=[".WEBM"]
    )
    assert [i.service_date for i in custom.discover(None)] == [date(2026, 11, 8)]


@pytest.mark.parametrize(
    "options",
    [
        {"date_format": "%m.%d"},
        {"stable_minutes": -1},
        {"video_extensions": []},
        {"unknown": 1},
        {"folder_id": ""},
    ],
)
def test_invalid_options(drive: FakeDrive, options: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        make_input(drive, **options)


def discovered(drive: FakeDrive) -> SourceItem:
    service_folder(drive, "SPX 11.01.26", ("b.mp4", b"second part"), ("a.mp4", b"first part"))
    (item,) = make_input(drive).discover(None)
    return item


def test_fetch_downloads_parts_in_order(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    dest = tmp_path / "parts"
    paths = make_input(drive).fetch(item, dest)
    assert [p.name for p in paths] == ["01-a.mp4", "02-b.mp4"]
    assert [p.read_bytes() for p in paths] == [b"first part", b"second part"]
    assert sorted(p.name for p in dest.iterdir()) == ["01-a.mp4", "02-b.mp4"]
    media = [r for r in drive.requests if r.url.params.get("alt") == "media"]
    assert len(media) == 2


def test_fetch_reuses_verified_parts(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    source = make_input(drive)
    first = source.fetch(item, tmp_path)
    mtimes = [p.stat().st_mtime_ns for p in first]
    first[1].write_bytes(b"second parT")
    again = source.fetch(item, tmp_path)
    assert again == first and again[1].read_bytes() == b"second part"
    assert again[0].stat().st_mtime_ns == mtimes[0]
    media = [r for r in drive.requests if r.url.params.get("alt") == "media"]
    assert len(media) == 3


def test_fetch_checksum_mismatch_leaves_no_file(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    drive.corrupt_downloads = True
    with pytest.raises(ModuleError, match="downloaded") as exc:
        make_input(drive).fetch(item, tmp_path)
    assert exc.value.retryable
    assert list(tmp_path.iterdir()) == []
    bad = item.parts[0].model_copy(update={"checksum": "0" * 32})
    drive.files[bad.ref].reported_md5 = "0" * 32
    drive.corrupt_downloads = False
    with pytest.raises(ModuleError, match="md5") as exc:
        make_input(drive).fetch(item.model_copy(update={"parts": (bad,)}), tmp_path)
    assert exc.value.retryable
    assert list(tmp_path.iterdir()) == []


def test_fetch_detects_changes_since_discovery(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    drive.files[item.parts[1].ref].content = b"re-uploaded"
    with pytest.raises(SourceNotReadyError, match="changed"):
        make_input(drive).fetch(item, tmp_path)
    del drive.files[item.parts[1].ref]
    with pytest.raises(SourceNotReadyError, match="no longer exists"):
        make_input(drive).fetch(item, tmp_path)


def test_fetch_media_streams_parts_in_order(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    media = make_input(drive).fetch_media(item, tmp_path)
    assert [type(m).__name__ for m in media] == ["RemoteMedia", "RemoteMedia"]
    assert [m.name for m in media] == ["01-a.mp4", "02-b.mp4"]
    urls = [m.url for m in media]  # type: ignore[attr-defined]
    assert urls == [
        f"https://www.googleapis.com/drive/v3/files/{p.ref}?alt=media&supportsAllDrives=true"
        for p in item.parts
    ]
    assert media[0].key != media[1].key  # type: ignore[attr-defined]
    assert list(tmp_path.rglob("*.mp4")) == []  # nothing downloaded
    assert media[0].headers()["Authorization"] == "Bearer test-token"  # type: ignore[attr-defined]
    media_calls = [r for r in drive.requests if r.url.params.get("alt") == "media"]
    assert media_calls == []


def test_fetch_media_detects_changes_since_discovery(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    drive.files[item.parts[1].ref].content = b"re-uploaded"
    with pytest.raises(SourceNotReadyError, match="changed"):
        make_input(drive).fetch_media(item, tmp_path)


def test_fetch_retries_transient_errors(drive: FakeDrive, tmp_path: Path) -> None:
    item = discovered(drive)
    drive.failures = [("alt=media", 503), ("alt=media", 429)]
    paths = make_input(drive).fetch(item, tmp_path)
    assert paths[0].read_bytes() == b"first part"
    drive.failures = [("alt=media", 404)]
    with pytest.raises(ModuleError) as exc:
        make_input(drive).fetch(item, tmp_path / "other")
    assert not exc.value.retryable


def test_folder_listing_query(drive: FakeDrive) -> None:
    list(make_input(drive, include_shared_drives=False).discover(None))
    (call,) = drive.calls("GET", "/drive/v3/files")
    assert call.url.params["q"] == (
        f"'{ROOT}' in parents and trashed = false and mimeType = '{FOLDER_MIME}'"
    )
    assert "supportsAllDrives" not in call.url.params


def test_registry_builds_from_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from georg.pipeline.registry import build_input

    monkeypatch.setenv("GEORG_TEST_DRIVE_FOLDER", "folder-from-env")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/key.json")
    config = NamedModuleConfig(
        name="drive", type="google_drive", options={"folder_id": "env:GEORG_TEST_DRIVE_FOLDER"}
    )
    source = build_input(config)
    assert isinstance(source, GoogleDriveInput)
    assert source.folder_id == "folder-from-env"
    assert source.options.credentials_file == "env:GOOGLE_APPLICATION_CREDENTIALS"


def test_unset_folder_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEORG_TEST_DRIVE_FOLDER", raising=False)
    with pytest.raises(ConfigError, match="GEORG_TEST_DRIVE_FOLDER"):
        GoogleDriveInput.from_options({"folder_id": "env:GEORG_TEST_DRIVE_FOLDER"})


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_fetch_document_downloads_once_per_version(drive: FakeDrive, tmp_path: Path) -> None:
    folder = service_folder(drive, "SPX 11.08.26", ("service.mp4", b"video"))
    name = "2026-11-08_Twenty-third_Sunday_after_Trinity_-_DS1-Eric.docx"
    doc = drive.add(name, folder, b"docx bytes", mime=DOCX_MIME, modified=OLD)
    drive.add("Order of Service", folder, mime="application/vnd.google-apps.document")
    source = make_input(drive)
    [item] = source.discover(None)
    dest = tmp_path / "documents"
    path = source.fetch_document(item, name, dest)
    assert path is not None and path.read_bytes() == b"docx bytes"
    assert doc.id in path.name and path.parent == dest
    assert source.fetch_document(item, name, dest) == path
    downloads = [r for r in drive.calls("GET", f"/files/{doc.id}") if "alt=media" in str(r.url)]
    assert len(downloads) == 1
    doc.content = b"edited"
    newer = source.fetch_document(item, name, dest)
    assert newer is not None and newer != path and newer.read_bytes() == b"edited"
    assert source.fetch_document(item, "Order of Service", dest) is None
    assert source.fetch_document(item, "missing.docx", dest) is None


class _BrokenTokens:
    def token(self, *, force_refresh: bool = False) -> str:
        raise ModuleError("token refresh refused")


def test_check_counts_the_folder_items(drive: FakeDrive) -> None:
    drive.folder("Recordings", id="check-root")
    drive.folder("SPX 11.01.26", parent="check-root")
    drive.add("notes.txt", "check-root", mime="text/plain")
    drive.add("deep.mp4", "elsewhere")
    result = make_input(drive, folder_id="check-root").check()
    assert result.ok and result.message == "Token refreshed; folder readable (2 items)."
    assert all(r.method == "GET" for r in drive.requests)


def test_check_reports_a_missing_folder_and_a_bad_token(drive: FakeDrive) -> None:
    result = make_input(drive, folder_id="nowhere").check()
    assert not result.ok and "not found" in result.message
    source = GoogleDriveInput(
        NamedModuleConfig(name="drive", type="google_drive", options={"folder_id": "x"}),
        credentials=_BrokenTokens(),
        http_client=drive.client(),
        sleep=lambda _: None,
    )
    broken = source.check()
    assert not broken.ok and broken.message.startswith("Token refresh failed")
