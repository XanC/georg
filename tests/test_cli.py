# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from georg import __version__
from georg.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_succeeds() -> None:
    assert main([]) == 0
