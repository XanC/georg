# SPDX-License-Identifier: AGPL-3.0-only
"""ffmpeg/ffprobe operations used by the pipeline.

Every function writes to a temporary sibling file and renames it into place, so a
crash never leaves a truncated output under the final name.
"""

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from georg.core.config import RenditionSpec
from georg.core.errors import ModuleError
from georg.core.types import ArtifactRef, TimeRange

FFMPEG = os.environ.get("GEORG_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("GEORG_FFPROBE", "ffprobe")


class MediaError(ModuleError):
    """ffmpeg or ffprobe failed, or produced output that does not check out."""


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    has_audio: bool
    has_video: bool
    sample_rate: int | None
    channels: int | None


def _run(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{args[0]} is not installed") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise MediaError(f"{Path(args[0]).name} failed ({proc.returncode}): {tail}")
    return proc.stdout


def _ffmpeg(*args: str) -> None:
    _run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args])


def _tmp(dest: Path) -> Path:
    return dest.with_name(f".{dest.stem}.partial{dest.suffix}")


def probe(path: Path) -> MediaInfo:
    out = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    data = json.loads(out)
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    has_video = any(
        s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        for s in streams
    )
    duration = data.get("format", {}).get("duration")
    if duration is None:
        raise MediaError(f"{path}: duration unknown")
    return MediaInfo(
        duration_ms=round(float(duration) * 1000),
        has_audio=audio is not None,
        has_video=has_video,
        sample_rate=int(audio["sample_rate"]) if audio and "sample_rate" in audio else None,
        channels=int(audio["channels"]) if audio and "channels" in audio else None,
    )


def extract_audio(
    parts: Sequence[Path], dest: Path, *, sample_rate: int = 44100, channels: int = 2
) -> Path:
    """Join the audio of `parts`, in order, into one lossless FLAC file."""
    if not parts:
        raise MediaError("no source parts")
    for part in parts:
        if not probe(part).has_audio:
            raise MediaError(f"{part}: no audio stream")
    inputs = [arg for part in parts for arg in ("-i", str(part))]
    fmt = f"aresample={sample_rate},aformat=sample_fmts=s16:channel_layouts="
    layout = "mono" if channels == 1 else "stereo"
    chains = "".join(f"[{i}:a:0]{fmt}{layout}[a{i}];" for i in range(len(parts)))
    joined = "".join(f"[a{i}]" for i in range(len(parts)))
    graph = f"{chains}{joined}concat=n={len(parts)}:v=0:a=1[out]"
    tmp = _tmp(dest)
    _ffmpeg(*inputs, "-filter_complex", graph, "-map", "[out]", "-c:a", "flac", str(tmp))
    tmp.replace(dest)
    return dest


def cut(src: Path, span: TimeRange, dest: Path, *, fade_ms: int = 0) -> Path:
    """Copy `span` of `src` into a FLAC file, with optional fade in and out."""
    filters = [f"atrim=start={span.start_ms / 1000:.3f}:end={span.end_ms / 1000:.3f}"]
    filters.append("asetpts=PTS-STARTPTS")
    if fade_ms:
        fade = min(fade_ms, span.duration_ms // 2) / 1000
        out_start = span.duration_ms / 1000 - fade
        filters.append(f"afade=t=in:d={fade:.3f},afade=t=out:st={out_start:.3f}:d={fade:.3f}")
    tmp = _tmp(dest)
    _ffmpeg("-i", str(src), "-af", ",".join(filters), "-c:a", "flac", str(tmp))
    tmp.replace(dest)
    return dest


def encode_mp3(
    src: Path, dest: Path, spec: RenditionSpec, *, tags: Mapping[str, str] | None = None
) -> Path:
    """Encode `src` to MP3 using the rendition's channels, rate, and bitrate mode."""
    args = ["-i", str(src), "-vn", "-map_metadata", "-1", "-c:a", "libmp3lame"]
    args += ["-ac", str(spec.channels), "-ar", str(spec.sample_rate)]
    if spec.vbr_quality is not None:
        args += ["-q:a", str(spec.vbr_quality)]
    else:
        args += ["-b:a", f"{spec.bitrate_kbps}k"]
    for key, value in (tags or {}).items():
        args += ["-metadata", f"{key}={value}"]
    tmp = _tmp(dest)
    _ffmpeg(*args, "-id3v2_version", "3", str(tmp))
    tmp.replace(dest)
    return dest


def verify_decode(path: Path, *, expected_ms: int | None = None, tolerance_ms: int = 250) -> int:
    """Decode the whole file; return its duration. Fail on errors or a length mismatch."""
    _run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
          "-f", "null", "-"])  # fmt: skip
    duration = probe(path).duration_ms
    if expected_ms is not None and abs(duration - expected_ms) > tolerance_ms:
        raise MediaError(f"{path}: duration {duration} ms, expected {expected_ms} ms")
    return duration


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_ref(path: Path, mime: str, *, expected_ms: int | None = None) -> ArtifactRef:
    """Verify a produced media file and describe it for the artifact store."""
    if path.stat().st_size == 0:
        raise MediaError(f"{path}: empty file")
    duration = verify_decode(path, expected_ms=expected_ms)
    return ArtifactRef(
        path=path,
        sha256=sha256_file(path),
        size=path.stat().st_size,
        mime=mime,
        duration_ms=duration,
    )
