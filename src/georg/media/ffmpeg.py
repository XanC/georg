# SPDX-License-Identifier: AGPL-3.0-only
"""ffmpeg/ffprobe operations used by the pipeline.

Every function writes to a temporary sibling file and renames it into place, so a
crash never leaves a truncated output under the final name. Inputs may be local
paths or `RemoteMedia` HTTP(S) sources; credentials sent in `-headers` are
redacted from errors.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from georg.core.config import RenditionSpec
from georg.core.errors import ModuleError
from georg.core.types import ArtifactRef, MediaInput, RemoteMedia, TimeRange

FFMPEG = os.environ.get("GEORG_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("GEORG_FFPROBE", "ffprobe")

_BEARER = re.compile(r"(?i)bearer\s+\S+")


def _scrub(text: str) -> str:
    """Remove bearer tokens before text reaches an error message or log."""
    return _BEARER.sub("Bearer <redacted>", text)


class MediaError(ModuleError):
    """ffmpeg or ffprobe failed, or produced output that does not check out."""


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    has_audio: bool
    has_video: bool
    sample_rate: int | None
    channels: int | None


def _run(args: Sequence[str], *, retryable: bool = False) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{args[0]} is not installed") from exc
    if proc.returncode != 0:
        tail = _scrub("\n".join(proc.stderr.strip().splitlines()[-5:]))
        raise MediaError(
            f"{Path(args[0]).name} failed ({proc.returncode}): {tail}", retryable=retryable
        )
    return proc.stdout


def _ffmpeg(*args: str, retryable: bool = False) -> None:
    _run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
        retryable=retryable,
    )


def _tmp(dest: Path) -> Path:
    return dest.with_name(f".{dest.stem}.partial{dest.suffix}")


def _headers_value(headers: Mapping[str, str]) -> str:
    """An ffmpeg `-headers` argument: `Name: value` lines ended by CRLF."""
    return "".join(f"{name}: {value}\r\n" for name, value in headers.items())


def _name(part: MediaInput) -> str:
    return part.name if isinstance(part, RemoteMedia) else str(part)


def _probe_args(part: MediaInput, *, force_refresh: bool = False) -> list[str]:
    args = [FFPROBE, "-v", "error"]
    if isinstance(part, RemoteMedia):
        args += ["-headers", _headers_value(part.headers(force_refresh=force_refresh))]
        target = part.url
    else:
        target = str(part)
    return [*args, "-print_format", "json", "-show_format", "-show_streams", target]


def probe(path: Path) -> MediaInfo:
    return _probe_data(_run(_probe_args(path)), path)


def probe_media(
    part: MediaInput, *, force_refresh: bool = False, retryable: bool = False
) -> MediaInfo:
    """`probe` for a `MediaInput`: ffprobe a local file or a remote URL."""
    if isinstance(part, RemoteMedia):
        out = _run(_probe_args(part, force_refresh=force_refresh), retryable=retryable)
        return _probe_data(out, Path(part.name))
    return probe(part)


def _probe_data(out: str, path: Path) -> MediaInfo:
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


def _input_args(part: MediaInput, *, force_refresh: bool = False) -> list[str]:
    if isinstance(part, RemoteMedia):
        return [
            "-headers",
            _headers_value(part.headers(force_refresh=force_refresh)),
            "-i",
            part.url,
        ]
    return ["-i", str(part)]


def _extract_once(
    parts: Sequence[MediaInput],
    dest: Path,
    *,
    sample_rate: int,
    channels: int,
    force_refresh: bool,
    remote: bool,
) -> None:
    for part in parts:
        if not probe_media(part, force_refresh=force_refresh, retryable=remote).has_audio:
            raise MediaError(f"{_name(part)}: no audio stream")
    inputs = [a for part in parts for a in _input_args(part, force_refresh=force_refresh)]
    fmt = f"aresample={sample_rate},aformat=sample_fmts=s16:channel_layouts="
    layout = "mono" if channels == 1 else "stereo"
    chains = "".join(f"[{i}:a:0]{fmt}{layout}[a{i}];" for i in range(len(parts)))
    joined = "".join(f"[a{i}]" for i in range(len(parts)))
    graph = f"{chains}{joined}concat=n={len(parts)}:v=0:a=1[out]"
    _ffmpeg(
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[out]",
        "-c:a",
        "flac",
        str(dest),
        retryable=remote,
    )


def extract_audio(
    parts: Sequence[MediaInput],
    dest: Path,
    *,
    sample_rate: int = 44100,
    channels: int = 2,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Join the audio of `parts`, in order, into one lossless FLAC file.

    With `RemoteMedia` parts ffmpeg streams each URL (range reads, so the moov
    atom's position does not matter) and only the audio lands on disk. A failed
    run is retried `attempts` times with refreshed credentials, then raises a
    retryable error so the job queue retries the stage; the partial output is
    always removed.
    """
    if not parts:
        raise MediaError("no source parts")
    remote = any(isinstance(part, RemoteMedia) for part in parts)
    tries = attempts if remote else 1
    tmp = _tmp(dest)
    for attempt in range(1, tries + 1):
        try:
            _extract_once(
                parts,
                tmp,
                sample_rate=sample_rate,
                channels=channels,
                force_refresh=attempt > 1,
                remote=remote,
            )
        except MediaError as exc:
            tmp.unlink(missing_ok=True)
            if not exc.retryable or attempt == tries:
                raise
            sleep(min(2.0**attempt, 30.0))
            continue
        tmp.replace(dest)
        return dest
    raise MediaError("unreachable")


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
