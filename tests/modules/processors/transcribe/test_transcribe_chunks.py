# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from georg.core.types import TimeRange, TranscriptSegment, TranscriptWord
from georg.modules.processors.transcribe import merge_chunks, plan_chunks


def tr(a: int, b: int) -> TimeRange:
    return TimeRange(start_ms=a, end_ms=b)


def seg(text: str, a: int, b: int) -> TranscriptSegment:
    return TranscriptSegment(
        text=text, range=tr(a, b), words=(TranscriptWord(text=text, range=tr(a, b)),)
    )


def test_plan_single_chunk_when_short() -> None:
    assert plan_chunks(5_000, 10_000, 1_000) == [tr(0, 5_000)]


def test_plan_overlapping_chunks_cover_duration() -> None:
    spans = plan_chunks(25_000, 10_000, 1_000)
    assert spans == [tr(0, 10_000), tr(9_000, 19_000), tr(18_000, 25_000)]


def test_plan_exact_fit_has_no_empty_tail() -> None:
    assert plan_chunks(19_000, 10_000, 1_000) == [tr(0, 10_000), tr(9_000, 19_000)]


def test_plan_rejects_overlap_too_large() -> None:
    with pytest.raises(ValueError):
        plan_chunks(10_000, 2_000, 1_000)


def test_merge_shifts_offsets_and_dedupes_overlap() -> None:
    # Chunks [0, 10s) and [8s, 18s) overlap in [8s, 10s); the cut is at 9s.
    first = [seg("a", 0, 4_000), seg("b", 4_000, 8_500), seg("dup", 8_600, 9_800)]
    second = [seg("dup", 600, 1_800), seg("c", 1_800, 6_000), seg("d", 6_000, 9_000)]
    merged = merge_chunks([(tr(0, 10_000), first), (tr(8_000, 18_000), second)])
    assert [s.text for s in merged] == ["a", "b", "dup", "c", "d"]
    dup = merged[2]
    assert dup.range == tr(8_600, 9_800)
    assert dup.words[0].range == tr(8_600, 9_800)
    assert merged[3].range == tr(9_800, 14_000)
    assert merged[4].words[0].range == tr(14_000, 17_000)


def test_merge_keeps_segment_straddling_cut_once() -> None:
    first = [seg("x", 8_000, 9_600)]  # midpoint 8.8 s, before the 9 s cut
    second = [seg("x", 0, 1_600)]  # shifted midpoint 8.8 s too, dropped
    merged = merge_chunks([(tr(0, 10_000), first), (tr(8_000, 18_000), second)])
    assert [s.range for s in merged] == [tr(8_000, 9_600)]
