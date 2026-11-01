# SPDX-License-Identifier: AGPL-3.0-only
import math
import random

import pytest

from georg.core.contracts import BoundaryDetector
from georg.core.errors import ModuleError
from georg.core.types import (
    AudioSegment,
    BoundaryProposal,
    SegmentLabel,
    TimeRange,
    Transcript,
    TranscriptSegment,
)
from georg.modules.processors.detect import HeuristicBoundaryDetector, HeuristicDetectorParams

MIN = 60_000
S = 1_000


def build(spec: list[tuple[SegmentLabel, float]], offset_ms: int = 0) -> list[AudioSegment]:
    """Segments from consecutive (label, minutes) pairs starting at `offset_ms`."""
    out = []
    pos = offset_ms
    for label, minutes in spec:
        end = pos + round(minutes * MIN)
        out.append(AudioSegment(range=TimeRange(start_ms=pos, end_ms=end), label=label))
        pos = end
    return out


TYPICAL: list[tuple[SegmentLabel, float]] = [
    ("silence", 3),
    ("noise", 2),
    ("music", 6),  # prelude, service starts 5:00
    ("speech", 3),
    ("music", 5),
    ("speech", 10),  # readings
    ("music", 3),  # 29:00-32:00
    ("speech", 18),  # sermon 32:00-50:00
    ("music", 4),
    ("speech", 10),
    ("music", 6),
    ("speech", 2),
    ("music", 3),  # postlude ends 75:00
    ("silence", 3),
]
TYPICAL_MS = 78 * MIN
TYPICAL_EXPECTED = (5 * MIN, 32 * MIN + 100, 50 * MIN, 75 * MIN + 10 * S)


def bounds(p: BoundaryProposal) -> tuple[int, int, int, int]:
    b = p.boundaries
    return (b.service_start_ms, b.sermon_start_ms, b.sermon_end_ms, b.service_end_ms)


def shifted(values: tuple[int, int, int, int], by: int) -> tuple[int, ...]:
    return tuple(v + by for v in values)


def transcript(*segs: tuple[float, float, float | None]) -> Transcript:
    """Transcript from (start_s, end_s, probability) triples."""
    return Transcript(
        language="en",
        provider="fake",
        segments=tuple(
            TranscriptSegment(
                text="words",
                range=TimeRange(start_ms=round(a * S), end_ms=round(b * S)),
                avg_logprob=None if p is None else math.log(p),
            )
            for a, b, p in segs
        ),
    )


def detect(
    segments: list[AudioSegment], duration_ms: int, tr: Transcript | None = None
) -> BoundaryProposal:
    return HeuristicBoundaryDetector().detect(segments, duration_ms, tr)


def test_satisfies_protocol() -> None:
    det = HeuristicBoundaryDetector()
    assert isinstance(det, BoundaryDetector)
    assert det.type_name == "heuristic"
    assert det.version


def test_typical_service_without_transcript() -> None:
    p = detect(build(TYPICAL), TYPICAL_MS)
    assert bounds(p) == TYPICAL_EXPECTED
    assert p.confidence == pytest.approx(0.8)
    assert "No transcript" in p.explanation
    assert "00:32:00.1" in p.explanation


def test_transcript_refines_sermon_end() -> None:
    tr = transcript(
        (1925, 2400, 0.97),
        (2400, 2990, 0.95),
        (2990, 3000, 0.5),  # mumbled, not confident
        (3010, 3200, 0.99),  # singing after the music resumes; ignored
    )
    p = detect(build(TYPICAL), TYPICAL_MS, tr)
    assert bounds(p) == (*TYPICAL_EXPECTED[:2], 2990 * S, TYPICAL_EXPECTED[3])
    assert p.confidence == pytest.approx(1.0)
    assert "confident transcript segment" in p.explanation


def test_transcript_without_confident_segment_falls_back_to_speech() -> None:
    tr = transcript((1925, 2990, 0.5), (2990, 3000, None))
    p = detect(build(TYPICAL), TYPICAL_MS, tr)
    assert bounds(p) == TYPICAL_EXPECTED
    assert p.confidence == pytest.approx(0.7)
    assert "No confident transcript segment" in p.explanation


def test_sermon_found_only_by_later_probe() -> None:
    spec: list[tuple[SegmentLabel, float]] = [
        ("music", 6),
        ("speech", 12),
        ("music", 2),
        ("speech", 8),  # sermon 20:00-28:00, only the 4/12 probe lands here
        ("music", 10),
        ("speech", 22),
        ("music", 12),
        ("silence", 3),
    ]
    p = detect(build(spec), 75 * MIN)
    assert bounds(p) == (0, 20 * MIN + 100, 28 * MIN, 72 * MIN + 10 * S)
    assert p.confidence == pytest.approx(0.9 * 0.8)
    assert "only at 0.333" in p.explanation


@pytest.mark.parametrize("offset_min", [100, 37])
def test_long_recording_with_leading_and_trailing_silence(offset_min: int) -> None:
    offset = offset_min * MIN
    duration = 4 * 60 * MIN
    p = detect(build(TYPICAL, offset_ms=offset), duration)
    assert bounds(p) == shifted(TYPICAL_EXPECTED, offset)
    assert "Long recording" in p.explanation


def test_long_recording_uses_last_service() -> None:
    earlier = build(TYPICAL, offset_ms=20 * MIN)
    later = build(TYPICAL, offset_ms=125 * MIN)
    p = detect(earlier + later, 4 * 60 * MIN)
    assert bounds(p) == shifted(TYPICAL_EXPECTED, 125 * MIN)
    assert "2 active stretch(es)" in p.explanation


def test_long_recording_without_quiet_windows() -> None:
    spec: list[tuple[SegmentLabel, float]] = [("music", 5), ("speech", 5)] * 18
    p = detect(build(spec), 180 * MIN)
    assert "no quiet stretches" in p.explanation
    assert p.confidence < 0.8


def test_no_speech_only_probe_uses_longest_music_free_speech() -> None:
    spec: list[tuple[SegmentLabel, float]] = [
        ("music", 10),
        ("speech", 8),
        ("music", 27),  # all three probes land in music
        ("speech", 15),
        ("music", 10),
        ("silence", 2),
    ]
    p = detect(build(spec), 72 * MIN)
    assert bounds(p) == (0, 45 * MIN, 60 * MIN, 70 * MIN + 10 * S)
    assert p.confidence == pytest.approx(0.3)
    assert "No speech-only probe" in p.explanation


def test_no_speech_at_all() -> None:
    p = detect(build([("silence", 5), ("music", 60), ("silence", 5)]), 70 * MIN)
    b = p.boundaries
    assert (b.service_start_ms, b.service_end_ms) == (5 * MIN, 65 * MIN + 10 * S)
    assert p.confidence < 0.2
    assert "middle third" in p.explanation


def test_very_short_sermon() -> None:
    spec: list[tuple[SegmentLabel, float]] = [("music", 25), ("speech", 4), ("music", 31)]
    p = detect(build(spec), 60 * MIN)
    assert bounds(p) == (0, 25 * MIN + 100, 29 * MIN, 60 * MIN)
    assert p.confidence == pytest.approx(0.8 * 0.5)
    assert "implausibly short" in p.explanation


def test_very_long_sermon() -> None:
    spec: list[tuple[SegmentLabel, float]] = [("music", 10), ("speech", 50), ("music", 10)]
    p = detect(build(spec), 72 * MIN)
    assert bounds(p) == (0, 10 * MIN + 100, 60 * MIN, 70 * MIN + 10 * S)
    assert p.confidence == pytest.approx(0.8 * 0.6)
    assert "implausibly long" in p.explanation


def test_parameters_change_behavior() -> None:
    det = HeuristicBoundaryDetector(
        HeuristicDetectorParams(end_music_tail_ms=0, sermon_start_offset_ms=0)
    )
    p = det.detect(build(TYPICAL), TYPICAL_MS, None)
    assert bounds(p) == (5 * MIN, 32 * MIN, 50 * MIN, 75 * MIN)


def test_from_options() -> None:
    det = HeuristicBoundaryDetector.from_options({"step_ms": 60_000})
    assert det.params.step_ms == 60_000
    with pytest.raises(ModuleError):
        HeuristicBoundaryDetector.from_options({"no_such_option": 1})
    with pytest.raises(ModuleError):
        HeuristicBoundaryDetector.from_options({"probe_fractions": [1.5]})


def test_empty_and_degenerate_inputs() -> None:
    p = detect([], 10 * MIN)
    assert p.confidence == 0.0
    assert bounds(p)[3] == 10 * MIN
    p = detect([], 2)
    assert bounds(p)[0] == 0 and bounds(p)[3] == 2
    with pytest.raises(ModuleError):
        detect([], 1)


def test_unsorted_overlapping_and_out_of_range_segments() -> None:
    segs = build(TYPICAL)
    messy = [
        *reversed(segs),
        AudioSegment(range=TimeRange(start_ms=40 * MIN, end_ms=41 * MIN), label="speech"),
        AudioSegment(range=TimeRange(start_ms=77 * MIN, end_ms=90 * MIN), label="music"),
    ]
    p = detect(messy, TYPICAL_MS)
    assert bounds(p)[:3] == TYPICAL_EXPECTED[:3]
    assert bounds(p)[3] <= TYPICAL_MS


def test_fragmented_segments_are_merged() -> None:
    fragmented = []
    for seg in build(TYPICAL):
        r = seg.range
        for start in range(r.start_ms, r.end_ms, 1_500):
            end = min(start + 1_500, r.end_ms)
            fragmented.append(
                AudioSegment(range=TimeRange(start_ms=start, end_ms=end), label=seg.label)
            )
    assert bounds(detect(fragmented, TYPICAL_MS)) == TYPICAL_EXPECTED


@pytest.mark.parametrize("seed", range(60))
def test_random_inputs_always_give_ordered_boundaries(seed: int) -> None:
    rng = random.Random(seed)
    labels: list[SegmentLabel] = ["speech", "music", "noise", "silence"]
    duration = rng.choice([5 * S, 3 * MIN, 70 * MIN, 200 * MIN])
    segs: list[AudioSegment] = []
    pos = 0
    while pos < duration:
        end = min(pos + rng.randint(1, 8 * MIN), duration)
        if rng.random() > 0.1:
            segs.append(
                AudioSegment(range=TimeRange(start_ms=pos, end_ms=end), label=rng.choice(labels))
            )
        pos = end
    tr = None
    if rng.random() < 0.5:
        tr = transcript(
            *(
                (t, t + 20, rng.random())
                for t in range(0, duration // S - 20, 30)
                if rng.random() < 0.7
            )
        )
    p = HeuristicBoundaryDetector().detect(segs, duration, tr)
    b = p.boundaries
    assert 0 <= b.service_start_ms <= b.sermon_start_ms < b.sermon_end_ms
    assert b.sermon_end_ms <= b.service_end_ms <= duration
    assert 0.0 <= p.confidence <= 1.0
    assert p.explanation
