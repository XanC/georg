# SPDX-License-Identifier: AGPL-3.0-only
"""Heuristic boundary detector, ported from the legacy `find_timing.pl` script.

The legacy script re-ran a speech/music classifier and ffmpeg on short windows of the
recording. This port works on the classifier segments cached for the whole recording
and, for the sermon end, on the cached full-service transcript. It is a pure function.

Steps (all thresholds are fields of `HeuristicDetectorParams`):

1. Recordings longer than `long_recording_ms` may hold several services or hours of
   idle recording. Windows of `quiet_window_ms` whose speech+music share is below
   `quiet_window_max_active_fraction` are quiet; the last run of active windows is the
   approximate service. Shorter recordings use the whole file.
2. Service start: within a window around the approximate start, the earlier of the
   first sustained speech and the first sustained music.
3. Service end: within a window around the approximate end, the last substantial
   segment, chained trailing speech/music, and a label-specific tail.
4. Sermon: probe short windows at fractions of the service until one is speech only
   (no music, more speech than silence/noise), walk outward in steps while probes have
   speech and no music, then refine both edges inside a border window. The end is
   the last confident transcript segment before music resumes (a segment running into
   the music ends with the last speech before it), or the last speech segment without
   a transcript.
   If that sermon is implausibly short or long, later probes are tried.

Gaps between segments count as silence.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from georg.core.errors import ModuleError
from georg.core.types import (
    AudioSegment,
    BoundaryProposal,
    SegmentLabel,
    ServiceBoundaries,
    Transcript,
)

_MIN = 60_000

# Multiplicative confidence penalties, applied once per detected problem.
_PENALTY_NO_SEGMENTS = 0.0
_PENALTY_NO_QUIET_STRUCTURE = 0.7
_PENALTY_START_FALLBACK = 0.7
_PENALTY_END_FALLBACK = 0.7
_PENALTY_LATER_PROBE = 0.9
_PENALTY_NO_PROBE = 0.3
_PENALTY_NO_SPEECH = 0.1
_PENALTY_NO_TRANSCRIPT = 0.8
_PENALTY_NO_CONFIDENT_SEGMENT = 0.7
_PENALTY_SHORT_SERMON = 0.5
_PENALTY_LONG_SERMON = 0.6


class HeuristicDetectorParams(BaseModel):
    """Tunable parameters. Defaults match the legacy POC; times are milliseconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    long_recording_ms: int = Field(default=150 * _MIN, gt=0)
    """Recordings at least this long are searched for the last service (POC: 2.5 h)."""
    quiet_window_ms: int = Field(default=10 * _MIN, gt=0)
    """Window size for the long-recording scan (POC: 10-minute volume samples)."""
    quiet_window_max_active_fraction: float = Field(default=0.05, ge=0.0, le=1.0)
    """A window is quiet if speech+music cover less than this share (POC: < -50 dB)."""
    min_service_ms: int = Field(default=0, ge=0)
    """Ignore active runs shorter than this when picking the last service (POC: 0)."""

    start_search_before_ms: int = Field(default=10 * _MIN, ge=0)
    start_search_after_ms: int = Field(default=40 * _MIN, gt=0)
    start_speech_confirm_ms: int = Field(default=5_000, ge=0)
    """A speech segment longer than this confirms the speech start."""
    start_speech_reset_ms: int = Field(default=60_000, gt=0)
    """Drop a tentative speech start after this long without speech."""
    start_music_confirm_ms: int = Field(default=20_000, ge=0)
    """A music segment longer than this confirms the music start."""
    start_music_reset_ms: int = Field(default=120_000, gt=0)
    """Drop a tentative music start after this long without music."""

    end_search_before_ms: int = Field(default=30 * _MIN, ge=0)
    end_search_after_ms: int = Field(default=20 * _MIN, ge=0)
    end_ignore_shorter_ms: int = Field(default=1_000, ge=0)
    """Speech or music segments shorter than this are skipped when looking for the end."""
    end_min_segment_ms: int = Field(default=5_000, ge=0)
    """Segments shorter than this (and silence/noise) do not end the service."""
    end_music_tail_ms: int = Field(default=10_000, ge=0)
    """Added after closing music to keep applause and ambience."""
    end_speech_tail_ms: int = Field(default=2_000, ge=0)
    """Added after closing speech to keep the end of announcements."""
    end_trailing_gap_ms: int = Field(default=30_000, ge=0)
    """Include later substantial speech or music within this gap of the current end."""
    end_noise_after_music: bool = Field(default=True)
    """Follow `noise` that continues a closing music segment (the classifier often
    labels organ postlude fades as noise). Noise after speech never counts, and
    noise alone never selects the end. A noise segment longer than
    `end_trailing_gap_ms` stops the chain, so after-service chatter is not pulled in."""
    end_noise_gap_ms: int = Field(default=1_000, ge=0)
    """A noise segment continues closing music only if it starts within this gap."""

    probe_fractions: tuple[float, ...] = Field(default=(5 / 12, 6 / 12, 4 / 12), min_length=1)
    """Positions within the service probed for a speech-only region, in order."""
    probe_ms: int = Field(default=6_000, gt=0)
    step_ms: int = Field(default=2 * _MIN, gt=0)
    border_window_ms: int = Field(default=5 * _MIN, gt=0)
    """Window in which each sermon edge is refined."""
    sermon_start_offset_ms: int = Field(default=100, ge=0)
    """Added to the first speech onset; the classifier tends to start a hair early."""
    sermon_start_pad_ms: int = Field(default=300, ge=0)
    """Subtracted from the sermon start to avoid clipping its opening words."""
    sermon_end_pad_ms: int = Field(default=1_000, ge=0)
    """Added to the sermon end to avoid clipping its closing words."""
    transcript_min_probability: float = Field(default=0.75, ge=0.0, le=1.0)
    """A transcript segment is confident if exp(avg_logprob) exceeds this."""

    min_sermon_ms: int = Field(default=5 * _MIN, ge=0)
    max_sermon_ms: int = Field(default=45 * _MIN, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "HeuristicDetectorParams":
        if any(not 0.0 < f < 1.0 for f in self.probe_fractions):
            raise ValueError("probe_fractions must lie strictly between 0 and 1")
        if self.min_sermon_ms >= self.max_sermon_ms:
            raise ValueError("min_sermon_ms must be less than max_sermon_ms")
        return self


@dataclass(frozen=True)
class _Seg:
    start: int
    end: int
    label: SegmentLabel

    @property
    def duration(self) -> int:
        return self.end - self.start


def _fmt(ms: int) -> str:
    s, ms_part = divmod(max(ms, 0), 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}.{ms_part // 100}"


def _normalize(segments: Iterable[AudioSegment], duration_ms: int) -> list[_Seg]:
    """Sort, clip to the recording, and merge touching segments with the same label."""
    out: list[_Seg] = []
    for seg in sorted(segments, key=lambda s: (s.range.start_ms, s.range.end_ms)):
        start = max(seg.range.start_ms, out[-1].end if out else 0)
        end = min(seg.range.end_ms, duration_ms)
        if end <= start:
            continue
        if out and out[-1].label == seg.label and out[-1].end == start:
            out[-1] = _Seg(out[-1].start, end, seg.label)
        else:
            out.append(_Seg(start, end, seg.label))
    return out


def _clip(segs: Sequence[_Seg], start: int, end: int) -> list[_Seg]:
    return [
        _Seg(max(s.start, start), min(s.end, end), s.label)
        for s in segs
        if s.end > start and s.start < end
    ]


def _coverage(segs: Sequence[_Seg], start: int, end: int) -> dict[SegmentLabel, int]:
    cov: dict[SegmentLabel, int] = {"speech": 0, "music": 0, "noise": 0, "silence": 0}
    covered = 0
    for s in _clip(segs, start, end):
        cov[s.label] += s.duration
        covered += s.duration
    cov["silence"] += max(end - start - covered, 0)
    return cov


class HeuristicBoundaryDetector:
    """`BoundaryDetector` (type "heuristic") porting the legacy timing script."""

    type_name = "heuristic"
    version = "heuristic-4"

    def __init__(self, params: HeuristicDetectorParams | None = None) -> None:
        self.params = params or HeuristicDetectorParams()

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "HeuristicBoundaryDetector":
        """Build from a `ModuleConfig.options` table."""
        try:
            return cls(HeuristicDetectorParams.model_validate(dict(options)))
        except ValidationError as exc:
            raise ModuleError(f"invalid heuristic detector options: {exc}") from exc

    def detect(
        self,
        segments: Sequence[AudioSegment],
        duration_ms: int,
        transcript: Transcript | None,
    ) -> BoundaryProposal:
        if duration_ms < 2:
            raise ModuleError(f"recording too short to detect boundaries ({duration_ms} ms)")
        return _Run(self.params, _normalize(segments, duration_ms), duration_ms, transcript).run()


class _Run:
    """State for one detection; collects notes and confidence penalties."""

    def __init__(
        self,
        params: HeuristicDetectorParams,
        segs: list[_Seg],
        duration: int,
        transcript: Transcript | None,
    ) -> None:
        self.p = params
        self.segs = segs
        self.duration = duration
        self.transcript = transcript
        self.confidence = 1.0
        self.notes: list[str] = []
        self.problems: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def problem(self, text: str, penalty: float) -> None:
        self.problems.append(text)
        self.confidence *= penalty

    def run(self) -> BoundaryProposal:
        if not any(s.label in ("speech", "music") for s in self.segs):
            self.problem(
                "No speech or music segments; proposing the whole recording with the sermon "
                "in the middle third.",
                _PENALTY_NO_SEGMENTS,
            )
            third = self.duration // 3
            return self.finish(0, third, self.duration - third, self.duration)

        approx_start, approx_end = self.approximate_service()
        service_start = self.service_start(approx_start)
        service_end = self.service_end(approx_end)
        if service_end - service_start < 2:
            self.problem(
                f"Refined service bounds ({_fmt(service_start)} to {_fmt(service_end)}) were "
                "inconsistent; using the approximate service.",
                _PENALTY_START_FALLBACK,
            )
            service_start, service_end = approx_start, approx_end
        self.note(
            f"Service {_fmt(service_start)} to {_fmt(service_end)} "
            f"({_fmt(service_end - service_start)})."
        )
        sermon_start, sermon_end = self.sermon(service_start, service_end)
        return self.finish(service_start, sermon_start, sermon_end, service_end)

    def finish(
        self, service_start: int, sermon_start: int, sermon_end: int, service_end: int
    ) -> BoundaryProposal:
        d = self.duration
        service_start = min(max(service_start, 0), d - 2)
        service_end = min(max(service_end, service_start + 2), d)
        sermon_start = min(max(sermon_start, service_start), service_end - 1)
        sermon_end = min(max(sermon_end, sermon_start + 1), service_end)
        explanation = " ".join(self.notes)
        if self.problems:
            explanation += " Confidence reduced: " + " ".join(self.problems)
        return BoundaryProposal(
            boundaries=ServiceBoundaries(
                service_start_ms=service_start,
                sermon_start_ms=sermon_start,
                sermon_end_ms=sermon_end,
                service_end_ms=service_end,
            ),
            confidence=round(min(max(self.confidence, 0.0), 1.0), 3),
            explanation=explanation.strip(),
        )

    def approximate_service(self) -> tuple[int, int]:
        p = self.p
        if self.duration < p.long_recording_ms:
            return 0, self.duration
        win = p.quiet_window_ms
        runs: list[tuple[int, int]] = []
        start: int | None = None
        pos = 0
        while pos < self.duration:
            end = min(pos + win, self.duration)
            cov = _coverage(self.segs, pos, end)
            active = (cov["speech"] + cov["music"]) / (end - pos) >= (
                p.quiet_window_max_active_fraction
            )
            if active and start is None:
                start = pos
            elif not active and start is not None:
                runs.append((start, pos))
                start = None
            pos = end
        if start is not None:
            runs.append((start, self.duration))
        if not runs or runs == [(0, self.duration)]:
            self.problem(
                f"Long recording ({_fmt(self.duration)}) has no quiet stretches to separate "
                "services; searched the whole recording.",
                _PENALTY_NO_QUIET_STRUCTURE,
            )
            return 0, self.duration
        long_runs = [r for r in runs if r[1] - r[0] >= p.min_service_ms]
        chosen = long_runs[-1] if long_runs else max(runs, key=lambda r: r[1] - r[0])
        self.note(
            f"Long recording ({_fmt(self.duration)}): {len(runs)} active stretch(es) found "
            f"with {win // 1000}s windows; using {_fmt(chosen[0])} to {_fmt(chosen[1])}."
        )
        return chosen

    def service_start(self, approx: int) -> int:
        p = self.p
        lo = max(approx - p.start_search_before_ms, 0)
        hi = min(approx + p.start_search_after_ms, self.duration)
        window = _clip(self.segs, lo, hi)

        speech_start = self._first_sustained(
            window, "speech", p.start_speech_confirm_ms, p.start_speech_reset_ms, None
        )
        music_start = self._first_sustained(
            window, "music", p.start_music_confirm_ms, p.start_music_reset_ms, speech_start
        )
        found = [t for t in (speech_start, music_start) if t is not None]
        if not found:
            self.problem(
                f"No speech or music near the approximate start {_fmt(approx)}; using it.",
                _PENALTY_START_FALLBACK,
            )
            return approx
        start = min(found)
        kind = "speech" if start == speech_start else "music"
        self.note(f"Service start {_fmt(start)}: first sustained {kind}.")
        return start

    @staticmethod
    def _first_sustained(
        window: Sequence[_Seg],
        label: SegmentLabel,
        confirm_ms: int,
        reset_ms: int,
        reset_until: int | None,
    ) -> int | None:
        """The POC's tentative/confirmed onset search for one label."""
        tentative: int | None = None
        last_seen = 0
        for s in window:
            if s.label == label:
                last_seen = s.end
                if tentative is None:
                    tentative = s.start
                if s.duration > confirm_ms:
                    return tentative
            elif (
                tentative is not None
                and (reset_until is None or s.start < reset_until)
                and s.end - last_seen > reset_ms
            ):
                tentative = None
        return tentative

    def service_end(self, approx: int) -> int:
        p = self.p
        lo = max(approx - p.end_search_before_ms, 0)
        hi = min(approx + p.end_search_after_ms, self.duration)
        candidates = _clip(self.segs, lo, hi)
        selected: _Seg | None = None
        for s in reversed(candidates):
            if s.label != "music" and s.duration < p.end_ignore_shorter_ms:
                continue
            if s.label in ("noise", "silence") or s.duration < p.end_min_segment_ms:
                continue
            selected = s
            break
        if selected is not None:
            included = [selected]
            current_end = selected.end
            noise_continuations = 0
            for s in candidates:
                if s.start < current_end:
                    continue
                gap = s.start - current_end
                if s.label in ("speech", "music"):
                    if gap > p.end_trailing_gap_ms or s.duration < p.end_ignore_shorter_ms:
                        continue
                elif s.label == "noise":
                    if not (
                        p.end_noise_after_music
                        and included[-1].label == "music"
                        and gap <= p.end_noise_gap_ms
                    ):
                        continue
                    if s.duration > p.end_trailing_gap_ms:
                        break
                    noise_continuations += 1
                else:
                    continue
                included.append(s)
                current_end = s.end
            last = included[-1]
            tail = p.end_speech_tail_ms if last.label == "speech" else p.end_music_tail_ms
            end = min(last.end + tail, self.duration)
            inclusion_note = (
                f"; included {len(included) - 1} trailing speech/music segment(s)"
                + (
                    f", {noise_continuations} continuing the postlude as noise"
                    if noise_continuations
                    else ""
                )
                if len(included) > 1
                else ""
            )
            self.note(
                f"Service end {_fmt(end)}: last {last.label} segment"
                f" plus {tail}ms tail{inclusion_note}."
            )
            return end
        self.problem(
            f"No substantial speech or music near the approximate end {_fmt(approx)}; using it.",
            _PENALTY_END_FALLBACK,
        )
        return approx

    def speech_only(self, pos: int, service_end: int) -> bool:
        end = min(pos + self.p.probe_ms, service_end)
        if end <= pos:
            return False
        cov = _coverage(self.segs, pos, end)
        return cov["music"] == 0 and cov["speech"] > cov["noise"] + cov["silence"]

    def walk(self, probe: int, step: int, service_start: int, service_end: int) -> int:
        """Last probe position, stepping from `probe`, that has speech and no music."""
        good = probe
        while True:
            pos = good + step
            end = min(pos + self.p.probe_ms, service_end)
            if pos < service_start or end <= pos:
                return good
            cov = _coverage(self.segs, pos, end)
            if cov["music"] or not cov["speech"]:
                return good
            good = pos

    def sermon(self, service_start: int, service_end: int) -> tuple[int, int]:
        p = self.p
        span = service_end - service_start
        found: list[tuple[int, int, list[str], list[str], float]] = []
        for i, fraction in enumerate(p.probe_fractions):
            pos = service_start + int(span * fraction)
            if not self.speech_only(pos, service_end) or any(a <= pos < b for a, b, *_ in found):
                continue
            notes, problems, confidence = self.notes, self.problems, self.confidence
            self.notes, self.problems, self.confidence = [], [], 1.0
            if i:
                self.problem(
                    f"Speech-only probe found only at {fraction:.3f} of the service "
                    f"({_fmt(pos)}), not at {p.probe_fractions[0]:.3f}.",
                    _PENALTY_LATER_PROBE,
                )
            else:
                self.note(f"Speech-only probe at {_fmt(pos)}.")
            start, end = self.sermon_from_probe(pos, service_start, service_end)
            found.append((start, end, self.notes, self.problems, self.confidence))
            self.notes, self.problems, self.confidence = notes, problems, confidence
            if p.min_sermon_ms <= end - start <= p.max_sermon_ms:
                break
        if not found:
            return self.sermon_without_probe(service_start, service_end)
        plausible = [c for c in found if p.min_sermon_ms <= c[1] - c[0] <= p.max_sermon_ms]
        start, end, notes, problems, confidence = (plausible or found[:1])[0]
        self.notes += notes
        self.problems += problems
        self.confidence *= confidence
        return start, end

    def sermon_from_probe(
        self, probe: int, service_start: int, service_end: int
    ) -> tuple[int, int]:
        p = self.p
        last_good = self.walk(probe, p.step_ms, service_start, service_end)
        upper = min(last_good + 2 * p.step_ms, service_end)
        first_good = self.walk(probe, -p.step_ms, service_start, service_end)
        lower = max(first_good - p.step_ms, service_start)
        self.note(
            f"Speech-only probes span {_fmt(first_good)} to {_fmt(last_good + p.probe_ms)} "
            f"in {p.step_ms // 1000}s steps."
        )

        start = self.sermon_start(lower, first_good, service_start)
        end = self.sermon_end(last_good, upper, start, service_end)
        length = end - start
        if length < p.min_sermon_ms:
            self.problem(
                f"Sermon is implausibly short ({_fmt(length)} < {_fmt(p.min_sermon_ms)}).",
                _PENALTY_SHORT_SERMON,
            )
        elif length > p.max_sermon_ms:
            self.problem(
                f"Sermon is implausibly long ({_fmt(length)} > {_fmt(p.max_sermon_ms)}).",
                _PENALTY_LONG_SERMON,
            )
        return start, end

    def sermon_start(self, lower: int, first_good: int, service_start: int) -> int:
        p = self.p
        hi = min(lower + p.border_window_ms, first_good + p.probe_ms)
        window = _clip(self.segs, lower, hi)
        music_ends = [s.end for s in window if s.label == "music" and s.end <= first_good]
        after = music_ends[-1] if music_ends else lower
        for s in window:
            if s.label == "speech" and s.end > after:
                base = max(s.start, after) + p.sermon_start_offset_ms
                reason = "after music" if music_ends else "in the border window"
                start = self._pad_sermon_start(base, service_start)
                self.note(f"Sermon start {_fmt(start)}: first speech {reason}.")
                return start
        start = self._pad_sermon_start(first_good, service_start)
        self.note(f"Sermon start {_fmt(start)}: earliest speech-only probe.")
        return start

    def sermon_end(self, last_good: int, upper: int, sermon_start: int, service_end: int) -> int:
        p = self.p
        lo = max(upper - p.border_window_ms, sermon_start + 1)
        music = [s for s in _clip(self.segs, last_good + p.probe_ms, upper) if s.label == "music"]
        limit = music[0].start if music else upper
        where = f"before music at {_fmt(limit)}" if music else f"before {_fmt(limit)}"

        if self.transcript is None:
            self.problem(
                "No transcript; sermon end taken from speech segments only.",
                _PENALTY_NO_TRANSCRIPT,
            )
        else:
            threshold = (
                math.log(p.transcript_min_probability)
                if p.transcript_min_probability
                else -math.inf
            )
            # Whisper often stretches the last segment ("Amen") over the following music;
            # such a segment ends with the last speech before the music.
            speech_ends = [s.end for s in _clip(self.segs, lo, limit) if s.label == "speech"]
            cut = speech_ends[-1] if speech_ends else limit
            confident = [
                min(seg.range.end_ms, cut) if seg.range.end_ms > limit else seg.range.end_ms
                for seg in self.transcript.segments
                if seg.avg_logprob is not None
                and seg.avg_logprob > threshold
                and lo < seg.range.end_ms
                and (seg.range.end_ms <= limit or (music and seg.range.start_ms < limit))
            ]
            if confident:
                end = self._pad_sermon_end(max(confident), service_end)
                self.note(
                    f"Sermon end {_fmt(end)}: last confident transcript segment {where}, "
                    f"with up to {p.sermon_end_pad_ms}ms padding."
                )
                return end
            self.problem(
                f"No confident transcript segment between {_fmt(lo)} and {_fmt(limit)}; "
                "sermon end taken from speech segments.",
                _PENALTY_NO_CONFIDENT_SEGMENT,
            )

        speech_ends = [s.end for s in _clip(self.segs, lo, limit) if s.label == "speech"]
        if speech_ends:
            end = self._pad_sermon_end(speech_ends[-1], service_end)
            self.note(
                f"Sermon end {_fmt(end)}: last speech segment {where}, "
                f"with up to {p.sermon_end_pad_ms}ms padding."
            )
            return end
        end = self._pad_sermon_end(
            max(min(last_good + p.probe_ms, limit), sermon_start + 1), service_end
        )
        self.note(
            f"Sermon end {_fmt(end)}: end of the last speech-only probe, "
            f"with up to {p.sermon_end_pad_ms}ms padding."
        )
        return end

    def _pad_sermon_start(self, start: int, service_start: int) -> int:
        p = self.p
        floor = service_start
        if self.transcript is not None:
            previous = [
                word.range.end_ms + 100
                for segment in self.transcript.segments
                for word in segment.words
                if word.range.end_ms <= start
            ]
            if previous:
                floor = max(floor, max(previous))
        return max(floor, start - p.sermon_start_pad_ms)

    def _pad_sermon_end(self, end: int, service_end: int) -> int:
        p = self.p
        caps = [service_end]
        if self.transcript is not None:
            next_words = [
                word.range.start_ms - 100
                for segment in self.transcript.segments
                for word in segment.words
                if word.range.start_ms > end
            ]
            if next_words:
                caps.append(min(next_words))
        next_audio = [
            segment.start
            for segment in self.segs
            if segment.label in ("speech", "music") and segment.start > end
        ]
        if next_audio:
            caps.append(min(next_audio))
        padded = min(end + p.sermon_end_pad_ms, *caps)
        self.note(f"Sermon end padding considered from {_fmt(end)} to {_fmt(padded)}.")
        return max(end, padded)

    def sermon_without_probe(self, service_start: int, service_end: int) -> tuple[int, int]:
        """Fallback: the music-free stretch of the service with the most speech."""
        bounds = [service_start]
        for s in _clip(self.segs, service_start, service_end):
            if s.label == "music":
                bounds += [s.start, s.end]
        bounds.append(service_end)
        best: tuple[int, int, int] | None = None
        for a, b in zip(bounds[::2], bounds[1::2], strict=True):
            speech = [s for s in _clip(self.segs, a, b) if s.label == "speech"]
            if not speech:
                continue
            total = sum(s.duration for s in speech)
            if best is None or total > best[0]:
                best = (total, speech[0].start, speech[-1].end)
        fractions = ", ".join(f"{f:.3f}" for f in self.p.probe_fractions)
        if best is None:
            self.problem(
                f"No speech-only probe at {fractions} of the service and no speech outside "
                "music; proposing the middle third of the service.",
                _PENALTY_NO_SPEECH,
            )
            third = (service_end - service_start) // 3
            return service_start + third, service_end - third
        _, start, end = best
        self.problem(
            f"No speech-only probe at {fractions} of the service; using the music-free "
            f"stretch with the most speech ({_fmt(start)} to {_fmt(end)}).",
            _PENALTY_NO_PROBE,
        )
        return start, end
