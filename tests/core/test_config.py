# SPDX-License-Identifier: AGPL-3.0-only
from datetime import date
from pathlib import Path

import pytest

from georg.core.config import load_config, resolve_secret
from georg.core.errors import ConfigError

EXAMPLE = Path(__file__).parents[2] / "georg.example.toml"


def test_example_config_loads() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.instance == "stpaul"
    assert {r.name for r in cfg.renditions} == {"sermon", "service_hq", "service_phone"}
    assert [o.name for o in cfg.outputs] == ["website", "phone", "archive"]
    assert cfg.review.require_approval


def test_discovery_start_date(tmp_path: Path) -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.discovery.start_date == date(2026, 11, 1)
    text = EXAMPLE.read_text().replace("[discovery]\nstart_date = 2026-11-01\n\n", "")
    without = tmp_path / "georg.toml"
    without.write_text(text)
    assert load_config(without).discovery.start_date is None


@pytest.mark.parametrize(
    "line",
    ['start_date = "not a date"', 'start_date = "2026/11/01"', "other = 2026-11-01"],
)
def test_discovery_start_date_rejected(tmp_path: Path, line: str) -> None:
    text = EXAMPLE.read_text().replace("start_date = 2026-11-01", line)
    bad = tmp_path / "georg.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError):
        load_config(bad)


def test_unknown_rendition_is_rejected(tmp_path: Path) -> None:
    text = EXAMPLE.read_text().replace('renditions = ["sermon"]', 'renditions = ["nope"]')
    bad = tmp_path / "georg.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="unknown renditions"):
        load_config(bad)


def test_preview_rendition_name_is_reserved(tmp_path: Path) -> None:
    text = (
        EXAMPLE.read_text()
        + '\n[[renditions]]\nname = "preview"\nspan = "service"\nbitrate_kbps = 32\n'
    )
    bad = tmp_path / "georg.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="reserved"):
        load_config(bad)


def test_resolve_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEORG_TEST_SECRET", "s3cret")
    assert resolve_secret("env:GEORG_TEST_SECRET") == "s3cret"
    assert resolve_secret("plain") == "plain"
    monkeypatch.delenv("GEORG_TEST_SECRET")
    with pytest.raises(ConfigError):
        resolve_secret("env:GEORG_TEST_SECRET")
