# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fake_drive import FOLDER_MIME, FakeDrive

from georg.core.config import OutputConfig
from georg.core.contracts import OutputTarget
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import ArtifactRef, ExternalRef, PublicationRequest, SermonMetadata
from georg.modules.outputs.google_drive_upload import (
    KEY_PROPERTY,
    SHA256_PROPERTY,
    GoogleDriveUploadOutput,
)

ARCHIVE = "archive-folder"
KEY = "georg:test:service:1:archive"


class FakeTokens:
    def token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


def make_output(
    drive: FakeDrive, client: httpx.Client | None = None, **options: Any
) -> GoogleDriveUploadOutput:
    opts = {"folder_id": ARCHIVE, "credentials_file": "env:GEORG_TEST_DRIVE_KEY", **options}
    config = OutputConfig(
        name="archive", type="google_drive_upload", options=opts, renditions=("service_hq",)
    )
    return GoogleDriveUploadOutput(
        config, credentials=FakeTokens(), http_client=client or drive.client(), sleep=lambda _: None
    )


def request(
    tmp_path: Path, content: bytes = b"ID3 hq service audio", **update: Any
) -> PublicationRequest:
    path = tmp_path / "service_hq.mp3"
    path.write_bytes(content)
    ref = ArtifactRef(
        path=path, sha256=hashlib.sha256(content).hexdigest(), size=len(content), mime="audio/mpeg"
    )
    req = PublicationRequest(
        publication_key=KEY,
        service_date=date(2026, 11, 1),
        metadata=SermonMetadata(),
        files={"service_hq": ref},
    )
    return req.model_copy(update=update)


def uploads(drive: FakeDrive) -> list[httpx.Request]:
    return [r for r in drive.requests if r.url.path.startswith("/upload/drive/v3/files")]


def test_conforms_to_output_target(drive: FakeDrive) -> None:
    out = make_output(drive)
    assert isinstance(out, OutputTarget)
    assert out.renditions == frozenset({"service_hq"}) and not out.wants_transcript


def test_publish_creates_file_with_key_in_new_subfolder(drive: FakeDrive, tmp_path: Path) -> None:
    out = make_output(drive, subfolder="SPX {mmddyy}", filename="{date} service.mp3")
    req = request(tmp_path)
    ref = out.publish(req)
    uploaded = drive.files[ref.external_id]
    folder = drive.files[uploaded.parents[0]]
    assert folder.name == "SPX 11.01.26" and folder.mime == FOLDER_MIME
    assert folder.parents == [ARCHIVE]
    assert uploaded.name == "2026-11-01 service.mp3" and uploaded.mime == "audio/mpeg"
    assert uploaded.content == b"ID3 hq service audio"
    assert uploaded.app_properties == {
        KEY_PROPERTY: KEY,
        SHA256_PROPERTY: req.files["service_hq"].sha256,
    }
    assert ref.target == "archive"
    assert ref.url == f"https://drive.google.com/file/d/{ref.external_id}/view"
    assert out.find_existing(KEY) == ref
    assert out.find_existing("georg:test:service:2:archive") is None
    (start,) = [r for r in uploads(drive) if r.method == "POST"]
    assert start.url.params["uploadType"] == "resumable"
    assert start.headers["x-upload-content-length"] == str(len(uploaded.content))


def test_existing_subfolder_is_reused(drive: FakeDrive, tmp_path: Path) -> None:
    folder = drive.folder("SPX 11.01.26", ARCHIVE)
    ref = make_output(drive, subfolder="SPX {mmddyy}").publish(request(tmp_path))
    assert drive.files[ref.external_id].parents == [folder.id]
    assert [f.name for f in drive.files.values() if f.mime == FOLDER_MIME] == ["SPX 11.01.26"]


def test_retry_after_success_does_not_upload_again(drive: FakeDrive, tmp_path: Path) -> None:
    out = make_output(drive)
    first = out.publish(request(tmp_path))
    second = out.publish(request(tmp_path))
    third = out.publish(request(tmp_path, existing=first))
    assert first == second == third
    assert len(uploads(drive)) == 1
    assert len([f for f in drive.files.values() if f.mime == "audio/mpeg"]) == 1


def test_republish_updates_existing_file(drive: FakeDrive, tmp_path: Path) -> None:
    out = make_output(drive)
    first = out.publish(request(tmp_path, b"first master"))
    second = out.publish(request(tmp_path, b"re-mastered audio"))
    assert second.external_id == first.external_id
    assert drive.files[first.external_id].content == b"re-mastered audio"
    (patch,) = [r for r in uploads(drive) if r.method == "PATCH"]
    assert patch.url.path.endswith(f"/{first.external_id}")
    assert len([f for f in drive.files.values() if f.mime == "audio/mpeg"]) == 1


def test_stale_existing_ref_falls_back_to_key_lookup(drive: FakeDrive, tmp_path: Path) -> None:
    out = make_output(drive)
    first = out.publish(request(tmp_path))
    stale = ExternalRef(target="archive", external_id="deleted-file")
    again = out.publish(request(tmp_path, b"new", existing=stale))
    assert again.external_id == first.external_id
    drive.files[first.external_id].trashed = True
    fresh = out.publish(request(tmp_path, b"newer", existing=first))
    assert fresh.external_id != first.external_id


def test_chunked_upload_resumes_after_interruption(drive: FakeDrive, tmp_path: Path) -> None:
    content = bytes(range(256)) * 2500
    drive.drop_chunk_once = True
    ref = make_output(drive, chunk_size=256 * 1024).publish(request(tmp_path, content))
    assert drive.files[ref.external_id].content == content
    ranges = [r.headers["content-range"] for r in drive.requests if r.method == "PUT"]
    assert ranges[0] == f"bytes 0-262143/{len(content)}"
    assert ranges[1] == f"bytes */{len(content)}"
    assert ranges[2] == f"bytes 131072-393215/{len(content)}"
    assert ranges[-1].endswith(f"-{len(content) - 1}/{len(content)}")


def test_checksum_mismatch_is_retryable_and_retry_fixes_it(
    drive: FakeDrive, tmp_path: Path
) -> None:
    def tampering(request: httpx.Request) -> httpx.Response:
        response = drive.handle(request)
        if request.method == "PUT" and response.status_code == 200:
            return httpx.Response(200, json={**response.json(), "md5Checksum": "0" * 32})
        return response

    broken = make_output(drive, httpx.Client(transport=httpx.MockTransport(tampering)))
    with pytest.raises(ModuleError, match="md5") as exc:
        broken.publish(request(tmp_path))
    assert exc.value.retryable
    ref = make_output(drive).publish(request(tmp_path))
    assert len([f for f in drive.files.values() if f.mime == "audio/mpeg"]) == 1
    assert drive.files[ref.external_id].content == b"ID3 hq service audio"


def test_transient_errors_are_retried(drive: FakeDrive, tmp_path: Path) -> None:
    drive.failures = [("upload/drive", 503), ("upload/session", 500), ("drive/v3/files", 429)]
    ref = make_output(drive).publish(request(tmp_path))
    assert drive.files[ref.external_id].content == b"ID3 hq service audio"


def test_request_errors(drive: FakeDrive, tmp_path: Path) -> None:
    out = make_output(drive)
    with pytest.raises(ModuleError) as exc:
        out.publish(request(tmp_path).model_copy(update={"files": {}}))
    assert not exc.value.retryable
    with pytest.raises(ModuleError, match="too long"):
        out.publish(request(tmp_path, publication_key="k" * 200))
    req = request(tmp_path)
    req.files["service_hq"].path.write_bytes(b"changed on disk")
    with pytest.raises(ModuleError, match="does not match"):
        out.publish(req)


@pytest.mark.parametrize(
    ("options", "renditions", "transcript"),
    [
        ({"filename": "{title}.mp3"}, ("service_hq",), False),
        ({"subfolder": "a/{date}"}, ("service_hq",), False),
        ({"chunk_size": 1000}, ("service_hq",), False),
        ({"extra": True}, ("service_hq",), False),
        ({}, ("sermon",), False),
        ({}, ("service_hq",), True),
    ],
)
def test_invalid_config(
    drive: FakeDrive, options: dict[str, Any], renditions: tuple[str, ...], transcript: bool
) -> None:
    opts = {"folder_id": ARCHIVE, "credentials_file": "env:X", **options}
    config = OutputConfig(
        name="archive",
        type="google_drive_upload",
        options=opts,
        renditions=renditions,
        transcript=transcript,
    )
    with pytest.raises(ConfigError):
        GoogleDriveUploadOutput(config, credentials=FakeTokens(), http_client=drive.client())


def test_folder_id_from_env(drive: FakeDrive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEORG_TEST_ARCHIVE_FOLDER", ARCHIVE)
    output = make_output(drive, folder_id="env:GEORG_TEST_ARCHIVE_FOLDER")
    assert output.folder_id == ARCHIVE
