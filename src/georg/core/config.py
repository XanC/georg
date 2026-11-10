# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration schema, loaded from a TOML file.

Secrets never appear in the file. An option value of the form "env:NAME" is a
reference to the environment variable NAME, resolved by `resolve_secret` when a
module is constructed.
"""

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from georg.core.errors import ConfigError

SECRET_PREFIX = "env:"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModuleConfig(_Section):
    type: str
    """Registered module type, e.g. "google_drive" or "lemonfox"."""
    options: dict[str, Any] = Field(default_factory=dict)


class NamedModuleConfig(ModuleConfig):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")


class OutputConfig(NamedModuleConfig):
    renditions: tuple[str, ...] = ()
    transcript: bool = False


class RenditionSpec(_Section):
    """An MP3 rendition cut from the mastered service audio."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    span: Literal["service", "sermon"]
    channels: Literal[1, 2] = 1
    sample_rate: int = 44100
    bitrate_kbps: int | None = None
    """Constant bitrate; mutually exclusive with `vbr_quality`."""
    vbr_quality: int | None = Field(default=None, ge=0, le=9)

    @model_validator(mode="after")
    def _one_rate_mode(self) -> "RenditionSpec":
        if (self.bitrate_kbps is None) == (self.vbr_quality is None):
            raise ValueError("set exactly one of bitrate_kbps or vbr_quality")
        return self


def default_renditions() -> tuple[RenditionSpec, ...]:
    """The renditions the St. Paul church machine has been producing."""
    return (
        RenditionSpec(name="sermon", span="sermon", channels=1, vbr_quality=2),
        RenditionSpec(name="service_hq", span="service", channels=2, vbr_quality=2),
        RenditionSpec(
            name="service_phone", span="service", channels=1, sample_rate=22050, bitrate_kbps=32
        ),
    )


class ProcessorsConfig(_Section):
    classifier: ModuleConfig
    detector: ModuleConfig
    transcriber: ModuleConfig
    formatter: ModuleConfig = ModuleConfig(type="llama_cpp")
    """Defaults to the local llama.cpp formatter, which needs no API key."""
    metadata: ModuleConfig


class SmtpConfig(_Section):
    host: str
    port: int = 587
    starttls: bool = True
    username: str | None = None
    password: str | None = None
    """Normally an "env:NAME" reference."""
    sender: str
    """From address, e.g. "Georg <georg@example.org>"."""


class ReviewConfig(_Section):
    require_approval: bool = True
    notify: tuple[str, ...] = ()
    """Email addresses told when a service is ready for review or a job fails for good."""
    base_url: str | None = None
    """Public address of the review web app, used for links in notices."""
    smtp: SmtpConfig | None = None


class DiscoveryConfig(_Section):
    start_date: date | None = None
    """Oldest service date automatic discovery picks up; unset means no cutoff.
    `georg discover --since` overrides it for one run."""


class GeorgConfig(_Section):
    instance: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    """Short stable name; part of every publication key, so never change it."""
    data_dir: Path
    timezone: str = "UTC"
    language: str = "en"
    inputs: tuple[NamedModuleConfig, ...] = Field(min_length=1)
    processors: ProcessorsConfig
    renditions: tuple[RenditionSpec, ...] = Field(default_factory=default_renditions)
    outputs: tuple[OutputConfig, ...] = ()
    review: ReviewConfig = ReviewConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    idle_shutdown_minutes: int | None = Field(default=None, gt=0)
    """Power the host off after this long with no jobs and no web requests (off if unset)."""
    idle_shutdown_command: tuple[str, ...] = Field(
        default=("sudo", "systemctl", "poweroff"), min_length=1
    )
    """Run by the worker's idle watchdog; see docs/deploy-aws.md for the container setup."""

    @model_validator(mode="after")
    def _references(self) -> "GeorgConfig":
        for group in (self.inputs, self.outputs, self.renditions):
            names = [item.name for item in group]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate names: {names}")
        known = {r.name for r in self.renditions}
        for out in self.outputs:
            missing = set(out.renditions) - known
            if missing:
                raise ValueError(f"output {out.name!r} uses unknown renditions {sorted(missing)}")
        return self


def load_config(path: Path) -> GeorgConfig:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
        return GeorgConfig.model_validate(raw)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def resolve_secret(value: str) -> str:
    """Return the value, or the referenced environment variable for "env:NAME"."""
    if not value.startswith(SECRET_PREFIX):
        return value
    name = value.removeprefix(SECRET_PREFIX)
    try:
        return os.environ[name]
    except KeyError:
        raise ConfigError(f"environment variable {name} is not set") from None
