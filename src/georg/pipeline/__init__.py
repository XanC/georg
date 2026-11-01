# SPDX-License-Identifier: AGPL-3.0-only
"""Builds configured modules and runs the processing stages over a recording."""

from georg.pipeline.local import LocalRunResult, StageCache, run_local
from georg.pipeline.registry import Processors, build_input, build_processor, build_processors
from georg.pipeline.service import discover, process_service
from georg.pipeline.worker import Worker

__all__ = [
    "LocalRunResult",
    "Processors",
    "StageCache",
    "Worker",
    "build_input",
    "build_processor",
    "build_processors",
    "discover",
    "process_service",
    "run_local",
]
