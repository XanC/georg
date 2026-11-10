# SPDX-License-Identifier: AGPL-3.0-only
import pytest
from pydantic import ValidationError

from georg.core.types import (
    ServiceBoundaries,
    SourceItem,
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
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
        segments=(seg(0, 10, "a"), seg(10, 20, "b"), seg(18, 40, "c")),
    )
    inner = t.within(TimeRange(start_ms=5, end_ms=25))
    # "a" and "b" overlap the span with midpoints inside; "c" is mostly outside.
    assert inner.text == "a b"


def word(start: int, end: int, text: str) -> TranscriptWord:
    return TranscriptWord(text=text, range=TimeRange(start_ms=start, end_ms=end))


def test_within_trims_stretched_segment_to_words_inside() -> None:
    # From a real service: the sermon's closing sentence ran into announcements
    # in one segment; only the sermon words should survive.
    seg = TranscriptSegment(
        text="our Lord. Amen. For the right of the reception",
        range=TimeRange(start_ms=2000, end_ms=3000),
        words=(
            word(2000, 2100, " our"),
            word(2100, 2200, " Lord."),
            word(2200, 2300, " Amen."),
            word(2900, 3000, " For"),
            word(3000, 3100, " the"),
        ),
    )
    t = Transcript(language="en", provider="fake", segments=(seg,))
    inner = t.within(TimeRange(start_ms=1000, end_ms=2800))
    assert len(inner.segments) == 1
    kept = inner.segments[0]
    assert kept.text == "our Lord. Amen."
    assert kept.range == TimeRange(start_ms=2000, end_ms=2300)
    assert [w.text for w in kept.words] == [" our", " Lord.", " Amen."]


def test_within_keeps_first_sentence_stretching_before_sermon_start() -> None:
    seg = TranscriptSegment(
        text="Dear Saints, there are a lot of temptations",
        range=TimeRange(start_ms=500, end_ms=3000),
        words=(
            word(500, 700, " Dear"),
            word(700, 1000, " Saints,"),
            word(1500, 1800, " there"),
            word(1800, 2100, " are"),
        ),
    )
    t = Transcript(language="en", provider="fake", segments=(seg,))
    inner = t.within(TimeRange(start_ms=1200, end_ms=5000))
    assert inner.segments[0].text == "there are"
    assert inner.segments[0].range == TimeRange(start_ms=1500, end_ms=2100)


def test_within_drops_segment_with_no_words_inside() -> None:
    seg = TranscriptSegment(
        text="For the right of the reception",
        range=TimeRange(start_ms=2000, end_ms=3000),
        words=(word(2000, 2100, " For"), word(2100, 2200, " the")),
    )
    t = Transcript(language="en", provider="fake", segments=(seg,))
    assert t.within(TimeRange(start_ms=3000, end_ms=5000)).segments == ()
