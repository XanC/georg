# SPDX-License-Identifier: AGPL-3.0-only
"""Builds configured modules and runs the processing stages over a recording."""

from georg.pipeline.local import LocalRunResult, StageCache, run_local
from georg.pipeline.registry import Processors, build_processor, build_processors

__all__ = [
    "LocalRunResult",
    "Processors",
    "StageCache",
    "build_processor",
    "build_processors",
    "run_local",
]
