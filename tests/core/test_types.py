# SPDX-License-Identifier: AGPL-3.0-only
import pytest
from pydantic import ValidationError

from georg.core.types import (
    ServiceBoundaries,
    SourceItem,
    TimeRange,
    Transcript,
    TranscriptSegment,
)


def test_time_range_rejects_empty_or_reversed() -> None:
    with pytest.raises(ValidationError):
        TimeRange(start_ms=5, end_ms=5)
    with pytest.raises(ValidationError):
        TimeRange(start_ms=-1, end_ms=5)
    assert TimeRange(start_ms=1000, end_ms=4000).duration_ms == 3000


def test_boundaries_must_be_ordered() -> None:
    b = ServiceBoundaries(
        service_start_ms=0, sermon_start_ms=1200, sermon_end_ms=2400, service_end_ms=3600
    )
    assert b.sermon == TimeRange(start_ms=1200, end_ms=2400)
    assert b.service.duration_ms == 3600
    with pytest.raises(ValidationError):
        ServiceBoundaries(
            service_start_ms=0, sermon_start_ms=2400, sermon_end_ms=2400, service_end_ms=3600
        )
    with pytest.raises(ValidationError):
        ServiceBoundaries(
            service_start_ms=0, sermon_start_ms=10, sermon_end_ms=20, service_end_ms=15
        )


def test_source_item_requires_a_part() -> None:
    with pytest.raises(ValidationError):
        SourceItem(source_type="google_drive", source_ref="x", parts=())


def test_transcript_within_keeps_contained_segments() -> None:
    def seg(a: int, b: int, text: str) -> TranscriptSegment:
        return TranscriptSegment(text=text, range=TimeRange(start_ms=a, end_ms=b))

    t = Transcript(
        language="en",
        provider="fake",
        segments=(seg(0, 10, "a"), seg(10, 20, "b"), seg(15, 30, "c")),
    )
    inner = t.within(TimeRange(start_ms=5, end_ms=25))
    assert inner.text == "b"
