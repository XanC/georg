# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Sequence

from georg.core.contracts import BoundaryDetector
from georg.core.types import (
    AudioSegment,
    BoundaryProposal,
    ServiceBoundaries,
    Transcript,
)


class FixedDetector:
    version = "fixed-1"

    def detect(
        self,
        segments: Sequence[AudioSegment],
        duration_ms: int,
        transcript: Transcript | None,
    ) -> BoundaryProposal:
        return BoundaryProposal(
            boundaries=ServiceBoundaries(
                service_start_ms=0,
                sermon_start_ms=duration_ms // 3,
                sermon_end_ms=2 * duration_ms // 3,
                service_end_ms=duration_ms,
            ),
            confidence=0.5,
            explanation="thirds",
        )


def test_structural_conformance() -> None:
    detector: BoundaryDetector = FixedDetector()
    assert isinstance(detector, BoundaryDetector)
    assert detector.detect([], 3000, None).boundaries.sermon.duration_ms == 1000
