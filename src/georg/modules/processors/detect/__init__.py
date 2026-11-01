# SPDX-License-Identifier: AGPL-3.0-only
"""Boundary detectors: propose service and sermon boundaries from cached analysis."""

from georg.modules.processors.detect.heuristic import (
    HeuristicBoundaryDetector,
    HeuristicDetectorParams,
)

__all__ = ["HeuristicBoundaryDetector", "HeuristicDetectorParams"]
