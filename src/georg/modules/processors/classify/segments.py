# SPDX-License-Identifier: AGPL-3.0-only
"""Helpers shared by classifiers: normalizing raw labels and (de)serializing segments."""

from collections.abc import Iterable

from pydantic import TypeAdapter, ValidationError

from georg.core.errors import ModuleError
from georg.core.types import AudioSegment, SegmentLabel, TimeRange

_SEGMENTS = TypeAdapter(list[AudioSegment])


def normalize_segments(
    raw: Iterable[tuple[SegmentLabel, int, int]], duration_ms: int
) -> list[AudioSegment]:
    """Turn possibly unsorted, overlapping, or gappy `(label, start_ms, end_ms)` spans
    into sorted, non-overlapping segments that exactly cover `[0, duration_ms)`.

    Gaps are absorbed by the preceding segment (or the first one, for a leading gap);
    overlaps are resolved in favor of the earlier segment; adjacent equal labels merge.
    A file with no spans at all is one silence segment.
    """
    if duration_ms <= 0:
        return []
    spans = sorted(
        (max(0, start), min(end, duration_ms), label)
        for label, start, end in raw
        if min(end, duration_ms) > max(0, start)
    )
    bounds: list[list[int]] = []
    labels: list[SegmentLabel] = []
    for start, end, label in spans:
        if bounds and end <= bounds[-1][1]:
            continue
        if not bounds:
            start = 0
        elif labels[-1] == label:
            bounds[-1][1] = end
            continue
        else:
            start = max(start, bounds[-1][1])
            bounds[-1][1] = start
        bounds.append([start, end])
        labels.append(label)
    if not bounds:
        return [AudioSegment(range=TimeRange(start_ms=0, end_ms=duration_ms), label="silence")]
    bounds[-1][1] = duration_ms
    return [
        AudioSegment(range=TimeRange(start_ms=s, end_ms=e), label=label)
        for (s, e), label in zip(bounds, labels, strict=True)
    ]


def segments_to_json(segments: Iterable[AudioSegment]) -> str:
    return _SEGMENTS.dump_json(list(segments)).decode()


def segments_from_json(data: str | bytes) -> list[AudioSegment]:
    try:
        return _SEGMENTS.validate_json(data)
    except ValidationError as exc:
        raise ModuleError(f"invalid segments JSON: {exc}") from exc
