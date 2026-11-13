# SPDX-License-Identifier: AGPL-3.0-only
"""File-drop output: upload one rendition to a templated path over SFTP.

The upload goes to a temporary name in the target directory, its size is checked,
and it is then renamed over the final name, so readers never see a partial file.
"""

import posixpath
import stat
import string
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import paramiko
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from georg.core.checks import CheckResult
from georg.core.config import OutputConfig, resolve_secret
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import ArtifactRef, ExternalRef, PublicationRequest

TEMPLATE_FIELDS = frozenset({"date"})


class SftpOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    password: str | None = None
    """Usually "env:NAME"."""
    key_path: Path | None = None
    key_passphrase: str | None = None
    """Usually "env:NAME"."""
    known_hosts: Path | None = None
    """known_hosts file to trust; None uses the system and user known_hosts files."""
    remote_path: str
    """Destination, e.g. "/var/www/html/phone/services/{date}.mp3"; {date} is YYYY-MM-DD."""
    public_url: str | None = None
    """Optional URL template with the same fields, reported in the `ExternalRef`."""
    rendition: str = "service_phone"
    timeout_s: float = Field(default=30.0, gt=0)

    @field_validator("remote_path", "public_url")
    @classmethod
    def _template(cls, value: str | None) -> str | None:
        if value is None:
            return value
        fields = {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
        if not fields <= TEMPLATE_FIELDS:
            raise ValueError(f"unknown template fields {sorted(fields - TEMPLATE_FIELDS)}")
        return value

    @field_validator("remote_path")
    @classmethod
    def _filename(cls, value: str) -> str:
        if not posixpath.basename(value):
            raise ValueError("remote_path must name a file")
        return value


class SftpClient(Protocol):
    """The subset of `paramiko.SFTPClient` this module uses."""

    def put(
        self, localpath: str, remotepath: str, callback: Any = ..., confirm: bool = ...
    ) -> Any: ...

    def stat(self, path: str) -> Any: ...

    def remove(self, path: str) -> None: ...

    def posix_rename(self, oldpath: str, newpath: str) -> None: ...


Connector = Callable[[], AbstractContextManager[SftpClient]]


def paramiko_connector(options: SftpOptions) -> Connector:
    """Connect with host-key verification always on (unknown hosts are rejected)."""
    password = resolve_secret(options.password) if options.password else None
    passphrase = resolve_secret(options.key_passphrase) if options.key_passphrase else None

    @contextmanager
    def connect() -> Iterator[SftpClient]:
        ssh = paramiko.SSHClient()
        try:
            if options.known_hosts is not None:
                ssh.load_host_keys(str(options.known_hosts.expanduser()))
            else:
                ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            ssh.connect(
                options.host,
                port=options.port,
                username=options.username,
                password=password,
                key_filename=str(options.key_path.expanduser()) if options.key_path else None,
                passphrase=passphrase,
                timeout=options.timeout_s,
                banner_timeout=options.timeout_s,
                auth_timeout=options.timeout_s,
                allow_agent=False,
                look_for_keys=options.key_path is None and password is None,
            )
            sftp = ssh.open_sftp()
            try:
                yield sftp
            finally:
                sftp.close()
        finally:
            ssh.close()

    return connect


def _module_error(exc: Exception, action: str) -> ModuleError:
    if isinstance(exc, paramiko.BadHostKeyException | paramiko.AuthenticationException):
        return ModuleError(f"sftp {action}: {exc}", retryable=False)
    if isinstance(exc, paramiko.SSHException) and "known_hosts" in str(exc):
        return ModuleError(f"sftp {action}: host key not trusted: {exc}", retryable=False)
    return ModuleError(f"sftp {action}: {exc}", retryable=True)


class SftpOutput:
    """`OutputTarget` of type "sftp"."""

    wants_transcript = False

    def __init__(self, config: OutputConfig, *, connector: Connector | None = None) -> None:
        try:
            self.options = SftpOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ConfigError(f"output {config.name!r}: {exc}") from exc
        if config.renditions and set(config.renditions) != {self.options.rendition}:
            raise ConfigError(
                f"output {config.name!r} uploads rendition {self.options.rendition!r} "
                f"but lists {list(config.renditions)}"
            )
        if config.transcript:
            raise ConfigError(f"output {config.name!r}: sftp output cannot publish transcripts")
        self.name = config.name
        self.renditions = frozenset({self.options.rendition})
        self._connect = connector or paramiko_connector(self.options)

    def remote_dir(self) -> str:
        """The fixed directory part of `remote_path`, before any {date} field."""
        head = self.options.remote_path.split("{", 1)[0]
        if head.endswith("/"):
            return head.rstrip("/") or "/"
        return posixpath.dirname(head) or "."

    def check(self) -> CheckResult:
        """Connect with the configured key and known_hosts, then stat the remote directory."""
        directory = self.remote_dir()
        try:
            with self._connect() as sftp:
                attrs = sftp.stat(directory)
        except FileNotFoundError:
            return CheckResult(False, f"Connected, but {directory} does not exist.")
        except (OSError, paramiko.SSHException, ConfigError) as exc:
            return CheckResult(False, str(_module_error(exc, "connect")))
        mode = getattr(attrs, "st_mode", None)
        if mode is not None and not stat.S_ISDIR(mode):
            return CheckResult(False, f"Connected, but {directory} is not a directory.")
        return CheckResult(True, f"Connected; {directory} exists. Nothing was written.")

    def remote_path(self, service_date: date) -> str:
        return self.options.remote_path.format(date=service_date.isoformat())

    def _ref(self, service_date: date) -> ExternalRef:
        url = self.options.public_url
        return ExternalRef(
            target=self.name,
            external_id=self.remote_path(service_date),
            url=url.format(date=service_date.isoformat()) if url else None,
        )

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        """Always None: a file drop has nowhere to record the key.

        The destination is derived from the request's date and `publish` overwrites
        it, so a missed lookup cannot create a duplicate. Use `find_published` to
        check a specific request.
        """
        return None

    def find_published(self, request: PublicationRequest) -> ExternalRef | None:
        """Return the remote file for `request` if it exists with the rendition's size."""
        artifact = self._artifact(request)
        path = self.remote_path(request.service_date)
        try:
            with self._connect() as sftp:
                size = sftp.stat(path).st_size
        except FileNotFoundError:
            return None
        except (OSError, paramiko.SSHException) as exc:
            raise _module_error(exc, f"stat {path}") from exc
        return self._ref(request.service_date) if size == artifact.size else None

    def publish(self, request: PublicationRequest) -> ExternalRef:
        artifact = self._artifact(request)
        final = self.remote_path(request.service_date)
        directory, base = posixpath.split(final)
        tmp = posixpath.join(directory, f".{base}.{uuid.uuid4().hex[:12]}.partial")
        try:
            with self._connect() as sftp:
                try:
                    sftp.put(str(artifact.path), tmp, confirm=False)
                    self._check_size(sftp, tmp, artifact)
                    sftp.posix_rename(tmp, final)
                except BaseException:
                    _remove_quietly(sftp, tmp)
                    raise
                self._check_size(sftp, final, artifact)
        except ModuleError:
            raise
        except (OSError, paramiko.SSHException) as exc:
            raise _module_error(exc, f"upload to {final}") from exc
        return self._ref(request.service_date)

    def _artifact(self, request: PublicationRequest) -> ArtifactRef:
        try:
            return request.files[self.options.rendition]
        except KeyError:
            raise ModuleError(
                f"output {self.name!r}: request has no {self.options.rendition!r} rendition"
            ) from None

    @staticmethod
    def _check_size(sftp: SftpClient, path: str, artifact: ArtifactRef) -> None:
        size = sftp.stat(path).st_size
        if size != artifact.size:
            raise ModuleError(
                f"sftp {path}: remote size {size} != local size {artifact.size}", retryable=True
            )


def _remove_quietly(sftp: SftpClient, path: str) -> None:
    with suppress(OSError, paramiko.SSHException):
        sftp.remove(path)
