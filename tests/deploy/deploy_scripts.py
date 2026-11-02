# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
from pathlib import Path
from types import ModuleType

AWS = Path(__file__).resolve().parents[2] / "deploy" / "aws"


def load(relative: str, name: str) -> ModuleType:
    """Import a deploy script that is not part of the georg package."""
    spec = importlib.util.spec_from_file_location(name, AWS / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
