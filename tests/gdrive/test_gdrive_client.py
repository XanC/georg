# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

import httpx
import pytest
from fake_drive import FakeDrive

from georg.core.errors import ModuleError
from georg.gdrive import DriveClient, parse_time, quote_query
from georg.gdrive.client import _next_offset


class FakeTokens:
    def __init__(self) -> None:
        self.refreshes = 0

    def token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refreshes += 1
        return f"token-{self.refreshes}"


def make_client(drive: FakeDrive, sleeps: list[float] | None = None) -> DriveClient:
    log = sleeps if sleeps is not None else []
    return DriveClient(FakeTokens(), drive.client(), attempts=3, sleep=log.append)


def test_helpers() -> None:
    assert quote_query("St. Paul's \\ folder") == "St. Paul\\'s \\\\ folder"
    assert _next_offset("bytes=0-262143") == 262144
    assert _next_offset(None) == 0
    parsed = parse_time("2026-11-01T18:23:45.123Z")
    assert parsed is not None and parsed.utcoffset() is not None
    assert parse_time("yesterday") is None and parse_time(None) is None


def test_list_files_follows_pages(drive: FakeDrive) -> None:
    for i in range(5):
        drive.add(f"v{i}.mp4", "root")
    names = [f["name"] for f in make_client(drive).list_files("'root' in parents", "id, name")]
    assert names == [f"v{i}.mp4" for i in range(5)]
    assert len(drive.calls("GET", "/drive/v3/files")) == 3


def test_transient_errors_retry_then_give_up(drive: FakeDrive) -> None:
    drive.add("v.mp4", "root", id="vid")
    sleeps: list[float] = []
    drive.failures = [("files/vid", 503), ("files/vid", 403)]
    client = make_client(drive, sleeps)
    assert client.get_file("vid", "id") == {"id": "vid"}
    assert sleeps == [2.0, 4.0]
    drive.failures = [("files/vid", 500)] * 3
    with pytest.raises(ModuleError, match="HTTP 500") as exc:
        client.get_file("vid", "id")
    assert exc.value.retryable


def test_client_errors_are_not_retryable_and_hide_token(drive: FakeDrive) -> None:
    drive.failures = [("files", 400)]
    with pytest.raises(ModuleError) as exc:
        list(make_client(drive).list_files("'root' in parents", "id"))
    assert not exc.value.retryable and "token" not in str(exc.value)


def test_unauthorized_refreshes_token_once(drive: FakeDrive) -> None:
    drive.add("v.mp4", "root", id="vid")
    tokens = FakeTokens()
    drive.failures = [("files/vid", 401)]
    client = DriveClient(tokens, drive.client(), sleep=lambda _: None)
    assert client.get_file("vid", "id") == {"id": "vid"}
    assert tokens.refreshes == 1
    assert drive.requests[-1].headers["authorization"] == "Bearer token-1"


def test_missing_file_is_none(drive: FakeDrive) -> None:
    assert make_client(drive).get_file("nope", "id") is None


def test_transport_errors_are_retryable(tmp_path: Path) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = DriveClient(
        FakeTokens(), httpx.Client(transport=httpx.MockTransport(broken)), sleep=lambda _: None
    )
    with pytest.raises(ModuleError, match="ConnectError") as exc:
        client.download("vid", tmp_path / "x")
    assert exc.value.retryable


def test_quota_error_explains_shared_drives(drive: FakeDrive, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "error": {
                "code": 403,
                "message": "quota",
                "errors": [{"reason": "storageQuotaExceeded"}],
            }
        }
        return httpx.Response(403, json=body)

    client = DriveClient(FakeTokens(), httpx.Client(transport=httpx.MockTransport(handler)))
    path = tmp_path / "a.mp3"
    path.write_bytes(b"x")
    with pytest.raises(ModuleError, match="shared drive") as exc:
        client.upload(path, {"name": "a.mp3"}, mime="audio/mpeg", fields="id")
    assert not exc.value.retryable
