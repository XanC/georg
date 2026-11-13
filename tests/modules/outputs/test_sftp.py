# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import paramiko
import pytest

from georg.core.config import OutputConfig
from georg.core.contracts import OutputTarget
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import ArtifactRef, PublicationRequest, SermonMetadata
from georg.modules.outputs.sftp import SftpOutput

FINAL = "/var/www/html/phone/services/2026-11-01.mp3"


@dataclass
class Stat:
    st_size: int


@dataclass
class FakeSftp:
    """In-memory SFTP server; `truncate_puts` simulates a short upload."""

    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    truncate_puts: bool = False
    connections: int = 0

    def put(
        self, localpath: str, remotepath: str, callback: Any = None, confirm: bool = True
    ) -> Any:
        data = Path(localpath).read_bytes()
        self.files[remotepath] = data[:-1] if self.truncate_puts else data
        self.calls.append(("put", remotepath))
        return Stat(len(self.files[remotepath]))

    def stat(self, path: str) -> Stat:
        self.calls.append(("stat", path))
        if path not in self.files:
            raise FileNotFoundError(2, "No such file", path)
        return Stat(len(self.files[path]))

    def remove(self, path: str) -> None:
        self.calls.append(("remove", path))
        del self.files[path]

    def posix_rename(self, oldpath: str, newpath: str) -> None:
        self.calls.append(("rename", oldpath, newpath))
        self.files[newpath] = self.files.pop(oldpath)

    @contextmanager
    def connect(self) -> Iterator["FakeSftp"]:
        self.connections += 1
        yield self


def make_output(fake: FakeSftp, **options: Any) -> SftpOutput:
    opts = {
        "host": "web.example.org",
        "username": "georg",
        "known_hosts": "/etc/georg/known_hosts",
        "remote_path": "/var/www/html/phone/services/{date}.mp3",
        "public_url": "https://example.org/phone/services/{date}.mp3",
        **options,
    }
    config = OutputConfig(name="phone", type="sftp", options=opts, renditions=("service_phone",))
    return SftpOutput(config, connector=fake.connect)


def request(tmp_path: Path, content: bytes = b"ID3 fake mp3 payload") -> PublicationRequest:
    path = tmp_path / "service_phone.mp3"
    path.write_bytes(content)
    ref = ArtifactRef(
        path=path, sha256=hashlib.sha256(content).hexdigest(), size=len(content), mime="audio/mpeg"
    )
    return PublicationRequest(
        publication_key="georg:test:service:1:phone",
        service_date=date(2026, 11, 1),
        metadata=SermonMetadata(),
        files={"service_phone": ref},
    )


def test_conforms_to_output_target() -> None:
    out = make_output(FakeSftp())
    assert isinstance(out, OutputTarget)
    assert out.renditions == frozenset({"service_phone"}) and not out.wants_transcript


def test_publish_uploads_to_temp_name_then_renames(tmp_path: Path) -> None:
    fake = FakeSftp()
    req = request(tmp_path)
    ref = make_output(fake).publish(req)
    assert ref.target == "phone" and ref.external_id == FINAL
    assert ref.url == "https://example.org/phone/services/2026-11-01.mp3"
    assert fake.files == {FINAL: req.files["service_phone"].path.read_bytes()}
    put = [c for c in fake.calls if c[0] == "put"]
    assert len(put) == 1
    tmp = put[0][1]
    assert tmp != FINAL and tmp.startswith("/var/www/html/phone/services/.2026-11-01.mp3.")
    assert ("stat", tmp) in fake.calls
    assert ("rename", tmp, FINAL) in fake.calls
    assert fake.calls.index(("stat", tmp)) < fake.calls.index(("rename", tmp, FINAL))


def test_size_mismatch_fails_and_leaves_no_files(tmp_path: Path) -> None:
    fake = FakeSftp(truncate_puts=True, files={FINAL: b"old"})
    with pytest.raises(ModuleError, match="remote size") as exc:
        make_output(fake).publish(request(tmp_path))
    assert exc.value.retryable
    assert fake.files == {FINAL: b"old"}
    assert not any(c[0] == "rename" for c in fake.calls)


def test_republish_is_idempotent_and_overwrites(tmp_path: Path) -> None:
    fake = FakeSftp(files={FINAL: b"previous version"})
    out = make_output(fake)
    first = out.publish(request(tmp_path, b"new audio"))
    second = out.publish(request(tmp_path, b"new audio"))
    assert first == second
    assert fake.files == {FINAL: b"new audio"}


def test_find_published_checks_size(tmp_path: Path) -> None:
    fake = FakeSftp()
    out = make_output(fake)
    req = request(tmp_path, b"twelve bytes")
    assert out.find_published(req) is None
    fake.files[FINAL] = b"other"
    assert out.find_published(req) is None
    fake.files[FINAL] = b"twelve bytes"
    found = out.find_published(req)
    assert found is not None and found.external_id == FINAL
    assert out.find_existing(req.publication_key) is None


def test_missing_rendition_is_not_retryable(tmp_path: Path) -> None:
    req = request(tmp_path).model_copy(update={"files": {}})
    with pytest.raises(ModuleError) as exc:
        make_output(FakeSftp()).publish(req)
    assert not exc.value.retryable


def test_transport_errors_map_to_retryable(tmp_path: Path) -> None:
    @contextmanager
    def broken() -> Iterator[FakeSftp]:
        raise paramiko.SSHException("connection reset")
        yield FakeSftp()

    @contextmanager
    def untrusted() -> Iterator[FakeSftp]:
        raise paramiko.SSHException("Server 'web' not found in known_hosts")
        yield FakeSftp()

    config = make_output(FakeSftp())
    out = SftpOutput(
        OutputConfig(name="phone", type="sftp", options=config.options.model_dump()),
        connector=broken,
    )
    with pytest.raises(ModuleError) as exc:
        out.publish(request(tmp_path))
    assert exc.value.retryable
    out._connect = untrusted
    with pytest.raises(ModuleError, match="host key") as exc:
        out.publish(request(tmp_path))
    assert not exc.value.retryable


@pytest.mark.parametrize(
    "options",
    [
        {"remote_path": "/srv/{service}.mp3"},
        {"remote_path": "/srv/phone/"},
        {"surprise": True},
    ],
)
def test_invalid_options(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        make_output(FakeSftp(), **options)


def test_rejects_mismatched_renditions() -> None:
    config = OutputConfig(
        name="phone",
        type="sftp",
        options={"host": "h", "username": "u", "remote_path": "/x/{date}.mp3"},
        renditions=("sermon",),
    )
    with pytest.raises(ConfigError, match="rendition"):
        SftpOutput(config)


def test_paramiko_connector_rejects_unknown_host_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class FakeSSHClient:
        def load_host_keys(self, path: str) -> None:
            seen["known_hosts"] = path

        def load_system_host_keys(self) -> None:
            seen["system"] = True

        def set_missing_host_key_policy(self, policy: object) -> None:
            seen["policy"] = policy

        def connect(self, hostname: str, **kwargs: Any) -> None:
            seen["connect"] = (hostname, kwargs)
            raise paramiko.SSHException(f"Server {hostname!r} not found in known_hosts")

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setenv("GEORG_TEST_SFTP_PASSWORD", "pw")
    known = tmp_path / "known_hosts"
    config = OutputConfig(
        name="phone",
        type="sftp",
        options={
            "host": "web.example.org",
            "username": "georg",
            "password": "env:GEORG_TEST_SFTP_PASSWORD",
            "known_hosts": str(known),
            "remote_path": "/x/{date}.mp3",
        },
    )
    with pytest.raises(ModuleError, match="host key") as exc:
        SftpOutput(config).publish(request(tmp_path))
    assert not exc.value.retryable
    assert isinstance(seen["policy"], paramiko.RejectPolicy)
    assert seen["known_hosts"] == str(known) and "system" not in seen
    assert seen["connect"][1]["password"] == "pw" and seen["closed"]


@dataclass
class _DirStat:
    st_mode: int


class _StatOnly:
    def __init__(self, entries: dict[str, int]) -> None:
        self.entries = entries
        self.calls: list[str] = []

    def stat(self, path: str) -> _DirStat:
        self.calls.append(path)
        if path not in self.entries:
            raise FileNotFoundError(2, "No such file", path)
        return _DirStat(self.entries[path])

    @contextmanager
    def connect(self) -> Iterator["_StatOnly"]:
        yield self


def test_remote_dir_is_the_fixed_part_of_remote_path() -> None:
    fake = FakeSftp()
    assert make_output(fake).remote_dir() == "/var/www/html/phone/services"
    assert make_output(fake, remote_path="/srv/{date}/a.mp3").remote_dir() == "/srv"
    assert make_output(fake, remote_path="/srv/a-{date}.mp3").remote_dir() == "/srv"


def test_check_stats_the_remote_directory_without_writing() -> None:
    server = _StatOnly({"/var/www/html/phone/services": stat.S_IFDIR | 0o755})
    out = make_output(FakeSftp())
    out._connect = server.connect
    result = out.check()
    assert result.ok and "exists" in result.message
    assert server.calls == ["/var/www/html/phone/services"]
    server.entries = {"/var/www/html/phone/services": stat.S_IFREG | 0o644}
    assert "not a directory" in out.check().message
    server.entries = {}
    assert "does not exist" in out.check().message


def test_check_reports_connection_failures() -> None:
    @contextmanager
    def refused() -> Iterator[Any]:
        raise paramiko.AuthenticationException("Authentication failed.")
        yield

    out = make_output(FakeSftp())
    out._connect = refused
    result = out.check()
    assert not result.ok and "Authentication failed" in result.message
