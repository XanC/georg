# SPDX-License-Identifier: AGPL-3.0-only
"""The effective configuration as read-only display sections, with secrets masked.

Masking works on the dumped configuration before anything is formatted: an "env:NAME"
reference becomes "env:NAME (set)" or "env:NAME (missing)", and any other value under a
key that looks secret becomes "(hidden)". Secret values never reach a template.
"""

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from georg.core.config import SECRET_PREFIX, GeorgConfig

SECRET_KEY: Final = re.compile(r"password|secret|token|key|credential|private", re.IGNORECASE)
HIDDEN: Final = "(hidden)"
EMPTY: Final = "—"

Checkable = Callable[[str, str], bool]
"""(kind, module type) -> whether the module has a connection check."""


@dataclass(frozen=True)
class ModuleView:
    kind: str
    """"input", "output" or "processor"."""
    name: str
    type: str
    rows: list[tuple[str, str]]
    checkable: bool = False


@dataclass(frozen=True)
class SettingsSection:
    id: str
    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)
    modules: list[ModuleView] = field(default_factory=list)
    check: str | None = None
    """Connection check id for the whole section (e.g. "smtp")."""


def env_reference(value: str) -> str:
    name = value.removeprefix(SECRET_PREFIX)
    return f"{SECRET_PREFIX}{name} ({'set' if os.environ.get(name) else 'missing'})"


def mask(value: Any, key: str = "") -> Any:
    """`value` with every env reference described and every secret-looking key hidden."""
    if isinstance(value, str) and value.startswith(SECRET_PREFIX):
        return env_reference(value)
    if isinstance(value, Mapping):
        return {k: mask(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        if SECRET_KEY.search(key) and any(not _is_reference(v) for v in value):
            return HIDDEN
        return [mask(v, key) for v in value]
    if SECRET_KEY.search(key) and value not in (None, ""):
        return HIDDEN
    return value


def _is_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def secret_values(config: GeorgConfig) -> list[str]:
    """Every value `mask` hides: resolved env references and secret-looking literals.
    Used to redact connection check messages."""

    def walk(value: Any, key: str) -> Iterator[str]:
        if isinstance(value, str):
            if value.startswith(SECRET_PREFIX):
                resolved = os.environ.get(value.removeprefix(SECRET_PREFIX))
                if resolved:
                    yield resolved
            elif SECRET_KEY.search(key):
                yield value
        elif isinstance(value, Mapping):
            for k, v in value.items():
                yield from walk(v, str(k))
        elif isinstance(value, list | tuple):
            for v in value:
                yield from walk(v, key)

    return list(walk(config.model_dump(mode="json"), ""))


def fmt(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return EMPTY
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list) and all(not isinstance(v, dict | list) for v in value):
        return ", ".join(fmt(v) for v in value)
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def rows(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Masked `data` as (label, text) rows; nested tables become dotted labels."""
    return _rows(mask(data), "")


def _rows(data: Mapping[str, Any], prefix: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        label = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out += _rows(value, f"{label}.")
        else:
            out.append((label, fmt(value)))
    return out


def _rendition(spec: Mapping[str, Any]) -> str:
    rate = (
        f"CBR {spec['bitrate_kbps']} kbps"
        if spec.get("bitrate_kbps") is not None
        else f"VBR quality {spec['vbr_quality']}"
    )
    channels = "mono" if spec["channels"] == 1 else "stereo"
    return f"{spec['span']} span, {channels}, {spec['sample_rate']} Hz, {rate}"


def settings_sections(config: GeorgConfig, checkable: Checkable) -> list[SettingsSection]:
    data = config.model_dump(mode="json")
    processors = data["processors"]
    review = dict(data["review"])
    smtp = review.pop("smtp")
    return [
        SettingsSection(
            "general",
            "Instance",
            rows({k: data[k] for k in ("instance", "data_dir", "timezone", "language")}),
        ),
        SettingsSection(
            "inputs",
            "Inputs",
            modules=[
                ModuleView(
                    "input", i["name"], i["type"], rows(i["options"]), checkable("input", i["type"])
                )
                for i in data["inputs"]
            ],
        ),
        SettingsSection(
            "processors",
            "Processors",
            rows({k: v for k, v in processors.items() if not isinstance(v, dict | None)}),
            [
                ModuleView("processor", role, module["type"], rows(module["options"]))
                for role, module in processors.items()
                if isinstance(module, dict)
            ],
        ),
        SettingsSection(
            "renditions",
            "Renditions",
            [(r["name"], _rendition(r)) for r in data["renditions"]],
        ),
        SettingsSection(
            "outputs",
            "Outputs",
            modules=[
                ModuleView(
                    "output",
                    o["name"],
                    o["type"],
                    rows({"renditions": o["renditions"], "transcript": o["transcript"]})
                    + rows(o["options"]),
                    checkable("output", o["type"]),
                )
                for o in data["outputs"]
            ],
        ),
        SettingsSection("review", "Review", rows(review)),
        SettingsSection(
            "email",
            "Email (SMTP)",
            rows(smtp) if smtp else [("smtp", "not configured")],
            check="smtp" if smtp else None,
        ),
        SettingsSection("discovery", "Discovery", rows(data["discovery"])),
        SettingsSection("calendar", "Calendar", rows(data["calendar"])),
        SettingsSection(
            "idle",
            "Idle shutdown",
            rows(
                {
                    "idle_shutdown_minutes": data["idle_shutdown_minutes"],
                    "idle_shutdown_command": data["idle_shutdown_command"],
                }
            ),
        ),
    ]
