# SPDX-License-Identifier: AGPL-3.0-only
import subprocess
from typing import Any

import pytest

from wp_authority.wp import WP, WPError


def test_argv_local_and_ssh() -> None:
    assert WP().argv(["term", "list"]) == ["wp", "term", "list"]
    assert WP("sudo -u www-data php wp-cli.phar", "/var/www/html", "church").argv(
        ["term", "list", "a b"]
    ) == ["ssh", "church", "sudo -u www-data php wp-cli.phar term list 'a b' --path=/var/www/html"]


def test_php_payload_and_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["stdin"] = argv, kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, 'Notice: x\n@@WP-AUTHORITY@@{"ok": 1}\n', "")

    monkeypatch.setattr(subprocess, "run", run)
    assert WP().php("apply.php", {"actions": []}) == {"ok": 1}
    assert seen["argv"] == ["wp", "eval-file", "-"]
    assert "@PAYLOAD@" not in seen["stdin"] and seen["stdin"].startswith("<?php")


def test_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "boom")
    )
    with pytest.raises(WPError, match="boom"):
        WP().json(["term", "list", "x"])
