# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from georg.core.errors import ModuleError
from georg.core.types import AudioSegment, SegmentLabel, TimeRange
from georg.modules.processors.classify import (
    normalize_segments,
    segments_from_json,
    segments_to_json,
)


def spans(segments: list[AudioSegment]) -> list[tuple[SegmentLabel, int, int]]:
    return [(s.label, s.range.start_ms, s.range.end_ms) for s in segments]


def test_merges_adjacent_equal_labels_and_covers_file() -> None:
    raw: list[tuple[SegmentLabel, int, int]] = [
        ("speech", 0, 1000),
        ("speech", 1000, 2500),
        ("music", 2500, 4000),
        ("silence", 4000, 4980),
    ]
    assert spans(normalize_segments(raw, 5000)) == [
        ("speech", 0, 2500),
        ("music", 2500, 4000),
        ("silence", 4000, 5000),
    ]


def test_sorts_fills_gaps_and_trims_overlaps() -> None:
    raw: list[tuple[SegmentLabel, int, int]] = [
        ("music", 3000, 6000),
        ("speech", 200, 2000),
        ("noise", 2500, 3500),
        ("music", 5500, 7000),
        ("silence", 9000, 12000),
    ]
    assert spans(normalize_segments(raw, 10000)) == [
        ("speech", 0, 2500),
        ("noise", 2500, 3500),
        ("music", 3500, 9000),
        ("silence", 9000, 10000),
    ]


def test_drops_spans_contained_in_earlier_ones() -> None:
    raw: list[tuple[SegmentLabel, int, int]] = [("speech", 0, 5000), ("noise", 1000, 2000)]
    assert spans(normalize_segments(raw, 5000)) == [("speech", 0, 5000)]


def test_empty_input() -> None:
    assert spans(normalize_segments([], 3000)) == [("silence", 0, 3000)]
    assert normalize_segments([("speech", 0, 10)], 0) == []


def test_json_round_trip() -> None:
    segments = [
        AudioSegment(range=TimeRange(start_ms=0, end_ms=1500), label="silence"),
        AudioSegment(range=TimeRange(start_ms=1500, end_ms=90000), label="music", confidence=0.8),
        AudioSegment(range=TimeRange(start_ms=90000, end_ms=91234), label="speech"),
    ]
    text = segments_to_json(segments)
    assert segments_from_json(text) == segments
    assert segments_from_json(text.encode()) == segments
    assert segments_from_json(segments_to_json([])) == []


def test_json_rejects_invalid() -> None:
    with pytest.raises(ModuleError):
        segments_from_json('[{"range": {"start_ms": 5, "end_ms": 1}, "label": "speech"}]')
    with pytest.raises(ModuleError):
        segments_from_json('[{"range": {"start_ms": 0, "end_ms": 1}, "label": "applause"}]')
