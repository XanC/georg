# SPDX-License-Identifier: AGPL-3.0-only
"""ffmpeg/ffprobe operations used by the pipeline.

Every function writes to a temporary sibling file and renames it into place, so a
crash never leaves a truncated output under the final name. Inputs may be local
paths or `RemoteMedia` HTTP(S) sources. Remote sources are read through the
loopback `range_proxy`, which turns ffmpeg's open-ended range reads into closed
upstream ranges — Drive's download quota refuses open-ended requests with 403.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx

from georg.core.config import RenditionSpec
from georg.core.errors import ModuleError
from georg.core.types import ArtifactRef, MediaInput, RemoteMedia, TimeRange
from georg.media import range_proxy

FFMPEG = os.environ.get("GEORG_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("GEORG_FFPROBE", "ffprobe")

_BEARER = re.compile(r"(?i)bearer\s+\S+")
_HTTP_STATUS = re.compile(r"Server returned (\d{3})")
_QUOTA_REASONS = frozenset({"downloadQuotaExceeded", "userRateLimitExceeded", "rateLimitExceeded"})
DEFAULT_QUOTA_RETRY_AFTER = timedelta(hours=6)
"""Retry delay for quota/rate-limit refusals a `RemoteMedia` does not override."""


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


def _run(
    args: Sequence[str],
    *,
    retryable: bool = False,
    remote: RemoteMedia | None = None,
    force_refresh: bool = False,
) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{args[0]} is not installed") from exc
    if proc.returncode != 0:
        tail = _scrub("\n".join(proc.stderr.strip().splitlines()[-5:]))
        message = f"{Path(args[0]).name} failed ({proc.returncode}): {tail}"
        retry_after = None
        status = _http_status(proc.stderr)
        if remote is not None and status is not None and status >= 400:
            detail, retry_after = _remote_error(remote, force_refresh=force_refresh)
            message += detail
        raise MediaError(message, retryable=retryable, retry_after=retry_after)
    return proc.stdout


def _http_status(stderr: str) -> int | None:
    """The HTTP status ffmpeg says the server answered with, if any."""
    match = _HTTP_STATUS.search(stderr)
    return int(match[1]) if match else None


def _remote_error(
    part: RemoteMedia, *, force_refresh: bool = False
) -> tuple[str, timedelta | None]:
    """Ask the server why it refused; return (extra message text, retry_after).

    ffmpeg hides the response body, so a failed read only reports e.g. "Server
    returned 403 Forbidden". One small `Range: bytes=0-0` request with the part's
    headers reads the JSON error (Drive answers `{"error": {"message": ...,
    "errors": [{"reason": ...}]}}`). A quota or rate-limit reason, or a bare 429,
    asks the job queue to wait `part.quota_retry_after` before retrying.
    """
    try:
        response = httpx.get(
            part.url,
            headers={**part.headers(force_refresh=force_refresh), "Range": "bytes=0-0"},
            timeout=30.0,
            follow_redirects=True,
        )
        if response.status_code == 401 and not force_refresh:
            response = httpx.get(
                part.url,
                headers={**part.headers(force_refresh=True), "Range": "bytes=0-0"},
                timeout=30.0,
                follow_redirects=True,
            )
    except Exception:
        return "", None
    if response.status_code < 400:
        return "", None
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
    except ValueError:
        error = None
    if not isinstance(error, dict):
        return "", None
    reasons = {
        str(e.get("reason"))
        for e in error.get("errors", []) or []
        if isinstance(e, dict) and e.get("reason")
    }
    detail = _scrub(str(error.get("message") or ""))
    if reasons:
        detail += f" ({', '.join(sorted(reasons))})"
    retry_after = None
    if response.status_code == 429 or reasons & _QUOTA_REASONS:
        retry_after = part.quota_retry_after or DEFAULT_QUOTA_RETRY_AFTER
    return (f"; server answered {response.status_code}: {detail}" if detail else "", retry_after)


def _ffmpeg(
    *args: str,
    retryable: bool = False,
    remote: RemoteMedia | None = None,
    force_refresh: bool = False,
) -> None:
    _run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
        retryable=retryable,
        remote=remote,
        force_refresh=force_refresh,
    )


def _tmp(dest: Path) -> Path:
    return dest.with_name(f".{dest.stem}.partial{dest.suffix}")


def _name(part: MediaInput) -> str:
    return part.name if isinstance(part, RemoteMedia) else str(part)


def _probe_args(part: MediaInput, *, url: str | None = None) -> list[str]:
    target = url or (part.url if isinstance(part, RemoteMedia) else str(part))
    return [FFPROBE, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", target]  # fmt: skip


def probe(path: Path) -> MediaInfo:
    return _probe_data(_run(_probe_args(path)), path)


def probe_media(
    part: MediaInput,
    *,
    force_refresh: bool = False,
    retryable: bool = False,
    chunk_bytes: int = range_proxy.DEFAULT_CHUNK_BYTES,
) -> MediaInfo:
    """`probe` for a `MediaInput`: ffprobe a local file or a remote URL.

    Remote parts are probed through the loopback range proxy, so the upstream
    server only ever sees closed ranges.
    """
    if isinstance(part, RemoteMedia):
        with range_proxy.serve_remote(
            part, chunk_bytes=chunk_bytes, force_refresh=force_refresh
        ) as url:
            out = _run(
                _probe_args(part, url=url),
                retryable=retryable,
                remote=part,
                force_refresh=force_refresh,
            )
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


def _input_args(part: MediaInput, *, url: str | None = None) -> list[str]:
    if isinstance(part, RemoteMedia):
        return [
            "-reconnect", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_delay_max", "30",
            "-i", url or part.url,
        ]  # fmt: skip
    return ["-i", str(part)]


def duration_tolerance_ms(expected_ms: int) -> int:
    """How far an extracted file may fall short of the probed source total."""
    return max(2000, expected_ms // 200)


def _extract_once(
    parts: Sequence[MediaInput],
    dest: Path,
    *,
    sample_rate: int,
    channels: int,
    force_refresh: bool,
    remote: bool,
    probe_part: Callable[[MediaInput], MediaInfo],
    chunk_bytes: int,
) -> None:
    expected_ms = 0
    for part in parts:
        info = probe_part(part)
        if not info.has_audio:
            raise MediaError(f"{_name(part)}: no audio stream")
        expected_ms += info.duration_ms
    fmt = f"aresample={sample_rate},aformat=sample_fmts=s16:channel_layouts="
    layout = "mono" if channels == 1 else "stereo"
    chains = "".join(f"[{i}:a:0]{fmt}{layout}[a{i}];" for i in range(len(parts)))
    joined = "".join(f"[a{i}]" for i in range(len(parts)))
    graph = f"{chains}{joined}concat=n={len(parts)}:v=0:a=1[out]"
    with range_proxy.serve_all(
        list(parts), chunk_bytes=chunk_bytes, force_refresh=force_refresh
    ) as urls:
        inputs = [a for part in parts for a in _input_args(part, url=urls.get(id(part)))]
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
            remote=next((p for p in parts if isinstance(p, RemoteMedia)), None),
            force_refresh=force_refresh,
        )
    produced = probe(dest).duration_ms
    if produced < expected_ms - duration_tolerance_ms(expected_ms):
        raise MediaError(
            f"extracted {produced / 1000:.1f} s of expected {expected_ms / 1000:.1f} s",
            retryable=True,
        )


def extract_audio(
    parts: Sequence[MediaInput],
    dest: Path,
    *,
    sample_rate: int = 44100,
    channels: int = 2,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    remote_probes: Mapping[str, MediaInfo] | None = None,
    chunk_bytes: int = range_proxy.DEFAULT_CHUNK_BYTES,
) -> Path:
    """Join the audio of `parts`, in order, into one lossless FLAC file.

    With `RemoteMedia` parts ffmpeg streams each URL (range reads, so the moov
    atom's position does not matter) and only the audio lands on disk. A failed
    run is retried `attempts` times with refreshed credentials, then raises a
    retryable error so the job queue retries the stage; the partial output is
    always removed. An output much shorter than the probed source total (e.g.
    the server closed the stream early and ffmpeg took it for EOF) counts as a
    retryable failure too.

    `remote_probes` supplies already-known probe results keyed by
    `RemoteMedia.key` (the pipeline stage cache fills it); otherwise each remote
    part is probed once per call and the result is reused across the retries, so
    a part is downloaded from at most twice per attempt (probe + extract) and
    usually once.
    """
    if not parts:
        raise MediaError("no source parts")
    remote = any(isinstance(part, RemoteMedia) for part in parts)
    tries = attempts if remote else 1
    tmp = _tmp(dest)
    probes: dict[str, MediaInfo] = dict(remote_probes or {})

    def probe_part(part: MediaInput, *, force_refresh: bool = False) -> MediaInfo:
        if isinstance(part, RemoteMedia):
            if part.key not in probes:
                probes[part.key] = probe_media(
                    part,
                    force_refresh=force_refresh,
                    retryable=remote,
                    chunk_bytes=chunk_bytes,
                )
            return probes[part.key]
        return probe_media(part)

    for attempt in range(1, tries + 1):
        refresh = attempt > 1

        def probe_for_attempt(part: MediaInput, *, fresh: bool = refresh) -> MediaInfo:
            return probe_part(part, force_refresh=fresh)

        try:
            _extract_once(
                parts,
                tmp,
                sample_rate=sample_rate,
                channels=channels,
                force_refresh=refresh,
                remote=remote,
                probe_part=probe_for_attempt,
                chunk_bytes=chunk_bytes,
            )
        except MediaError as exc:
            tmp.unlink(missing_ok=True)
            # A quota/rate-limit failure (retry_after set) is not retried within
            # the call: it fails fast so the job queue waits `retry_after`.
            if not exc.retryable or attempt == tries or exc.retry_after is not None:
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
