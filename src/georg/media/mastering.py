# SPDX-License-Identifier: AGPL-3.0-only
"""ffmpeg-native mastering chain and MP3 renditions.

Replaces the church machine's SoX trim -> Mono compressor -> wavegain -> LAME chain
with optional high-pass -> `acompressor` -> two-pass EBU R128 `loudnorm`.
"""

import json
import math
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from georg.core.config import RenditionSpec
from georg.core.types import ArtifactRef, ServiceBoundaries, TimeRange
from georg.media import ffmpeg
from georg.media.ffmpeg import MediaError, artifact_ref, cut, encode_mp3, probe

MASTERING_VERSION = "1"
"""Bump whenever the chain's output for the same parameters changes."""

MP3_MIME = "audio/mpeg"


class MasteringParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    highpass_hz: float | None = Field(default=80.0, gt=0)
    """Remove rumble below this frequency; None disables the filter."""
    threshold_db: float = Field(default=-28.0, le=0, ge=-60)
    ratio: float = Field(default=3.0, ge=1, le=20)
    attack_ms: float = Field(default=15.0, ge=0.01, le=2000)
    release_ms: float = Field(default=300.0, ge=0.01, le=9000)
    makeup_db: float = Field(default=0.0, ge=0, le=36)
    knee_db: float = Field(default=9.0, ge=0, le=18)
    target_lufs: float = Field(default=-16.0, ge=-70, le=-5)
    true_peak_db: float = Field(default=-1.5, ge=-9, le=0)
    loudness_range: float = Field(default=11.0, ge=1, le=20)


class LoudnessStats(BaseModel):
    """EBU R128 measurements from `loudnorm`'s first pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    integrated_lufs: float
    true_peak_db: float
    loudness_range: float
    threshold_lufs: float
    target_offset: float


def _db_to_linear(db: float) -> float:
    return float(10 ** (db / 20))


def _pre_filters(params: MasteringParams) -> list[str]:
    filters = []
    if params.highpass_hz is not None:
        filters.append(f"highpass=f={params.highpass_hz:g}")
    filters.append(
        "acompressor="
        f"threshold={_db_to_linear(params.threshold_db):.6f}:ratio={params.ratio:g}"
        f":attack={params.attack_ms:g}:release={params.release_ms:g}"
        f":makeup={_db_to_linear(params.makeup_db):.6f}:knee={_db_to_linear(params.knee_db):.6f}"
    )
    return filters


def _loudnorm_targets(params: MasteringParams) -> str:
    return f"I={params.target_lufs:g}:TP={params.true_peak_db:g}:LRA={params.loudness_range:g}"


def _ffmpeg_stderr(args: Sequence[str]) -> str:
    cmd = [ffmpeg.FFMPEG, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} is not installed") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise MediaError(f"ffmpeg failed ({proc.returncode}): {tail}")
    return proc.stderr


def _parse_loudnorm(stderr: str) -> LoudnessStats:
    start, end = stderr.rfind("{"), stderr.rfind("}")
    if start < 0 or end < start:
        raise MediaError("loudnorm printed no measurements")
    try:
        raw = json.loads(stderr[start : end + 1])
        return LoudnessStats(
            integrated_lufs=float(raw["input_i"]),
            true_peak_db=float(raw["input_tp"]),
            loudness_range=float(raw["input_lra"]),
            threshold_lufs=float(raw["input_thresh"]),
            target_offset=float(raw["target_offset"]),
        )
    except (ValueError, KeyError) as exc:
        raise MediaError(f"unreadable loudnorm measurements: {exc}") from exc


def measure_loudness(
    src: Path, params: MasteringParams | None = None, *, filters: Sequence[str] = ()
) -> LoudnessStats:
    """Measure `src` after `filters`, relative to `params`' loudness targets."""
    params = params or MasteringParams()
    graph = ",".join([*filters, f"loudnorm={_loudnorm_targets(params)}:print_format=json"])
    return _parse_loudnorm(_ffmpeg_stderr(["-i", str(src), "-af", graph, "-vn", "-f", "null", "-"]))


def _require_audible(src: Path, stats: LoudnessStats) -> None:
    if not math.isfinite(stats.integrated_lufs) or not math.isfinite(stats.threshold_lufs):
        raise MediaError(f"{src}: no measurable loudness (silent input?)")


def _write_flac(src: Path, dest: Path, filters: Sequence[str]) -> Path:
    sample_rate = probe(src).sample_rate or 44100
    graph = ",".join([*filters, f"aresample={sample_rate}"])
    tmp = ffmpeg._tmp(dest)
    ffmpeg._ffmpeg("-i", str(src), "-vn", "-af", graph, "-c:a", "flac", str(tmp))
    tmp.replace(dest)
    return dest


def master(src: Path, dest: Path, params: MasteringParams | None = None) -> Path:
    """Compress and loudness-normalise `src` into a FLAC file at `dest`.

    Pass one measures the compressed signal; pass two applies `loudnorm` with those
    measurements, which keeps the gain linear whenever the targets allow it.
    """
    params = params or MasteringParams()
    pre = _pre_filters(params)
    stats = measure_loudness(src, params, filters=pre)
    _require_audible(src, stats)
    apply = (
        f"loudnorm={_loudnorm_targets(params)}"
        f":measured_I={stats.integrated_lufs:.2f}:measured_TP={stats.true_peak_db:.2f}"
        f":measured_LRA={stats.loudness_range:.2f}:measured_thresh={stats.threshold_lufs:.2f}"
        f":offset={stats.target_offset:.2f}:linear=true:print_format=none"
    )
    return _write_flac(src, dest, [*pre, apply])


def normalize_gain(src: Path, dest: Path, params: MasteringParams | None = None) -> Path:
    """Apply one linear gain toward the loudness target, never above the true-peak limit.

    No compression: a stand-in for the legacy `wavegain` step, used for A/B samples.
    """
    params = params or MasteringParams()
    stats = measure_loudness(src, params)
    _require_audible(src, stats)
    gain = min(params.target_lufs - stats.integrated_lufs, params.true_peak_db - stats.true_peak_db)
    return _write_flac(src, dest, [f"volume={gain:.2f}dB"])


def _span_in_file(
    span: TimeRange, origin_ms: int, duration_ms: int, tolerance_ms: int
) -> TimeRange:
    start = span.start_ms - origin_ms
    end = span.end_ms - origin_ms
    if start < 0 or end > duration_ms + tolerance_ms:
        raise MediaError(
            f"span {span.start_ms}-{span.end_ms} ms is outside the mastered audio "
            f"({origin_ms}-{origin_ms + duration_ms} ms)"
        )
    return TimeRange(start_ms=start, end_ms=min(end, duration_ms))


def render(
    mastered: Path,
    boundaries: ServiceBoundaries,
    specs: Sequence[RenditionSpec],
    out_dir: Path,
    *,
    origin_ms: int = 0,
    fade_ms: int = 300,
    tags: Mapping[str, str] | None = None,
    tolerance_ms: int = 250,
) -> dict[str, ArtifactRef]:
    """Cut each rendition's span from `mastered`, encode it, and verify the MP3.

    `origin_ms` is the source-timeline position of the first sample of `mastered`
    (normally `boundaries.service_start_ms`, since the service span is what gets
    mastered). Each output is `out_dir/<spec.name>.mp3`.
    """
    duration_ms = probe(mastered).duration_ms
    spans = {"service": boundaries.service, "sermon": boundaries.sermon}
    out_dir.mkdir(parents=True, exist_ok=True)
    cuts: dict[str, Path] = {}
    refs: dict[str, ArtifactRef] = {}
    try:
        for spec in specs:
            local = _span_in_file(spans[spec.span], origin_ms, duration_ms, tolerance_ms)
            if spec.span not in cuts:
                cuts[spec.span] = cut(
                    mastered, local, out_dir / f".{spec.span}.cut.flac", fade_ms=fade_ms
                )
            mp3 = encode_mp3(cuts[spec.span], out_dir / f"{spec.name}.mp3", spec, tags=tags)
            refs[spec.name] = artifact_ref(mp3, MP3_MIME, expected_ms=local.duration_ms)
    finally:
        for path in cuts.values():
            path.unlink(missing_ok=True)
    return refs
