# SPDX-License-Identifier: AGPL-3.0-only
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


def test_unknown_rendition_is_rejected(tmp_path: Path) -> None:
    text = EXAMPLE.read_text().replace('renditions = ["sermon"]', 'renditions = ["nope"]')
    bad = tmp_path / "georg.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="unknown renditions"):
        load_config(bad)


def test_resolve_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEORG_TEST_SECRET", "s3cret")
    assert resolve_secret("env:GEORG_TEST_SECRET") == "s3cret"
    assert resolve_secret("plain") == "plain"
    monkeypatch.delenv("GEORG_TEST_SECRET")
    with pytest.raises(ConfigError):
        resolve_secret("env:GEORG_TEST_SECRET")
