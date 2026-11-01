# SPDX-License-Identifier: AGPL-3.0-only
"""Splitting long audio into overlapping chunks and merging their transcripts."""

from collections.abc import Sequence

from georg.core.types import TimeRange, TranscriptSegment


def plan_chunks(duration_ms: int, chunk_ms: int, overlap_ms: int) -> list[TimeRange]:
    """Cover [0, duration_ms) with spans of at most `chunk_ms`, each overlapping the last."""
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    if chunk_ms <= 2 * overlap_ms:
        raise ValueError("chunk_ms must be more than twice overlap_ms")
    spans: list[TimeRange] = []
    start = 0
    while True:
        end = min(start + chunk_ms, duration_ms)
        spans.append(TimeRange(start_ms=start, end_ms=end))
        if end == duration_ms:
            return spans
        start = end - overlap_ms


def _shift_range(r: TimeRange, offset_ms: int) -> TimeRange:
    return TimeRange(start_ms=r.start_ms + offset_ms, end_ms=r.end_ms + offset_ms)


def _shift(segment: TranscriptSegment, offset_ms: int) -> TranscriptSegment:
    words = tuple(
        w.model_copy(update={"range": _shift_range(w.range, offset_ms)}) for w in segment.words
    )
    return segment.model_copy(
        update={"range": _shift_range(segment.range, offset_ms), "words": words}
    )


def _midpoint(r: TimeRange) -> float:
    return (r.start_ms + r.end_ms) / 2


def merge_chunks(
    chunks: Sequence[tuple[TimeRange, Sequence[TranscriptSegment]]],
) -> list[TranscriptSegment]:
    """Shift chunk-relative segments onto the input timeline and drop overlap duplicates.

    Where two consecutive chunks overlap, the cut is the middle of the overlap: a segment
    is kept from the chunk whose side of the cut holds the segment's midpoint. Segments at
    the far edge of a chunk, which the audio cut may have truncated, are therefore dropped
    in favour of the neighbour's copy.
    """
    merged: list[TranscriptSegment] = []
    for i, (span, segments) in enumerate(chunks):
        lower = float("-inf")
        upper = float("inf")
        if i > 0:
            prev = chunks[i - 1][0]
            lower = (span.start_ms + prev.end_ms) / 2
        if i + 1 < len(chunks):
            nxt = chunks[i + 1][0]
            upper = (nxt.start_ms + span.end_ms) / 2
        for segment in segments:
            shifted = _shift(segment, span.start_ms)
            if lower <= _midpoint(shifted.range) < upper:
                merged.append(shifted)
    merged.sort(key=lambda s: s.range.start_ms)
    return merged
