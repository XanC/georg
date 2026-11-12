# SPDX-License-Identifier: AGPL-3.0-only
"""Internal YAMNet frame scoring for the classify package.

YAMNet (Apache-2.0, Google) scores 521 AudioSet classes over ~0.96 s patches every
0.48 s. This module decodes audio to 16 kHz mono in bounded-memory chunks, scores
each frame, and maps the scores onto Georg's labels:

- ``music``: max class score over the AudioSet Music subtree (class-map indices
  132-276) plus Singing, Choir, Chant and Humming, so congregational singing and
  organ postludes count as music.
- ``speech``: Speech, Child speech, Conversation, Narration/monologue, Babbling,
  Whispering, Laughter and Speech synthesizer. Chatter, Crowd and Hubbub are
  deliberately not speech.
- ``silence``: the Silence class.
- ``noise``: the winning group scores below ``noise_score``, or anything else.

The label is the argmax over the three groups' best class score, and a
``majority_frames``-wide majority filter over the stitched frame sequence removes
sub-second flicker (it runs after chunk stitching, so runs crossing a chunk seam
are not clipped).

TensorFlow is an optional extra (`georg[classify]`) imported on first use. The
model is the TF2 SavedModel published as `google/yamnet/1` on tfhub.dev (a tar.gz
of a SavedModel plus `assets/yamnet_class_map.csv`); it is downloaded once into
``cache_dir`` under a name derived from the URL, so changing `model_url` fetches
a different model instead of silently reusing the old one.
"""

import csv
import gc
import hashlib
import io
import logging
import os
import subprocess
import tarfile
import urllib.request
import uuid
from array import array
from collections import Counter
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from georg.core.errors import ModuleError
from georg.core.types import SegmentLabel
from georg.media.ffmpeg import FFMPEG, MediaError
from georg.modules.processors.classify.ina import _clear_keras_session

log = logging.getLogger(__name__)

MODEL_URL = "https://tfhub.dev/google/yamnet/1?tf-hub-format=compressed"
"""Official YAMNet SavedModel archive (tfhub.dev serves a tar.gz)."""
_CLASS_MAP = Path("assets") / "yamnet_class_map.csv"

SAMPLE_RATE = 16_000
_PATCH_S = 0.96
"""Length of the audio patch each YAMNet frame scores."""
HOP_MS = 480
"""Step between consecutive YAMNet frames."""

MUSIC_INDEX_RANGE = range(132, 277)
"""Contiguous AudioSet class-map indices for the Music subtree."""
MUSIC_EXTRA_CLASSES = frozenset({"Singing", "Choir", "Chant", "Humming"})
"""Named music classes outside the index range (defensive; usually inside it)."""

SPEECH_CLASSES = frozenset(
    {
        "Speech",
        "Child speech, kid speaking",
        "Conversation",
        "Narration, monologue",
        "Babbling",
        "Whispering",
        "Laughter",
        "Speech synthesizer",
    }
)
"""Voice classes mapped to `speech`; Chatter/Crowd/Hubbub deliberately excluded."""

SILENCE_CLASSES = frozenset({"Silence"})


class YamnetOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_dir: str | None = None
    """Model cache; default `$KERAS_HOME/models` or `~/.keras/models`."""
    model_url: str = MODEL_URL
    """URL of the YAMNet SavedModel tar.gz; the cache directory name keys on it."""
    chunk_s: int = Field(default=600, ge=60)
    """Decode and score in windows of this many seconds to bound memory."""
    overlap_s: float = Field(default=2.0, ge=0.0)
    """Extra audio decoded past each chunk end for context; its frames are dropped."""
    noise_score: float = Field(default=0.1, ge=0.0)
    """Frames whose best group score is below this are labeled noise."""
    majority_frames: int = Field(default=5, ge=1)
    """Majority-filter window over stitched frame labels (odd values centered)."""

    def url_key(self) -> str:
        """Stable short key identifying `model_url`, for cache dir and version."""
        return hashlib.sha256(self.model_url.encode()).hexdigest()[:10]


def _library_version() -> str:
    try:
        return metadata.version("tensorflow")
    except metadata.PackageNotFoundError:
        return "missing"


def _cache_root(options: YamnetOptions) -> Path:
    if options.cache_dir:
        return Path(options.cache_dir).expanduser()
    keras = Path(os.environ.get("KERAS_HOME", Path.home() / ".keras"))
    return keras / "models"


def _model_dir(options: YamnetOptions) -> Path:
    return _cache_root(options) / f"yamnet-{options.url_key()}"


def _model_complete(model_dir: Path) -> bool:
    return (model_dir / "saved_model.pb").exists() and (model_dir / _CLASS_MAP).exists()


def _download_model(url: str, dest: Path) -> None:
    """Fetch and unpack the SavedModel archive, publishing it atomically.

    Each process extracts into its own temporary directory and publishes with a
    single rename after rechecking the destination, so concurrent first runs
    never observe a half-extracted model.
    """
    if _model_complete(dest):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.download-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with urllib.request.urlopen(url) as response:
            data = response.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        if not _model_complete(tmp):
            raise ModuleError(f"YAMNet archive from {url} is missing model files")
        try:
            tmp.rename(dest)
        except OSError:
            if not _model_complete(dest):
                raise
    except Exception as exc:
        raise ModuleError(
            f"could not download the YAMNet model from {url}: {exc}", retryable=True
        ) from exc
    finally:
        if tmp.exists():
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class YamnetModel(Protocol):
    """Callable interface the classifier needs: class names plus a scorer."""

    class_names: list[str]

    def __call__(self, samples: Sequence[float]) -> Sequence[Sequence[float]]:
        """Per-frame score rows (`(frames, len(class_names))`)."""
        ...


class _SavedModel:
    """Adapts the loaded TF SavedModel to `YamnetModel`."""

    def __init__(self, model: Any, class_names: list[str]) -> None:
        self._model = model
        self.class_names = class_names

    def __call__(self, samples: Sequence[float]) -> Sequence[Sequence[float]]:
        import tensorflow as tf

        scores, _embeddings, _spectrogram = self._model(tf.constant(samples))
        out: list[list[float]] = scores.numpy().tolist()
        return out


def load_model(options: YamnetOptions) -> YamnetModel:
    try:
        import tensorflow as tf
    except ImportError as exc:
        raise ModuleError(
            "tensorflow is not installed; install the 'classify' extra (uv sync --extra classify)"
        ) from exc
    model_dir = _model_dir(options)
    if not _model_complete(model_dir):
        _download_model(options.model_url, model_dir)
    try:
        model = tf.saved_model.load(str(model_dir))
        with open(model_dir / _CLASS_MAP, newline="") as f:
            class_names = [row["display_name"] for row in csv.DictReader(f)]
    except OSError as exc:
        raise ModuleError(f"could not load the YAMNet model in {model_dir}: {exc}") from exc
    return _SavedModel(model, class_names)


def release_model() -> None:
    """Drop Keras's global graph; callers should also drop their model reference."""
    _clear_keras_session()
    gc.collect()


def _decode(audio: Path, start_s: float, stop_s: float) -> "array[float]":
    """16 kHz mono float32 samples for [start_s, stop_s), via Georg's ffmpeg."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            str(start_s),
            "-t",
            str(stop_s - start_s),
            "-i",
            str(audio),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:]
        raise MediaError(f"ffmpeg failed ({proc.returncode}): {'; '.join(tail)}")
    samples: array[float] = array("f")
    samples.frombytes(proc.stdout)
    return samples


def _is_music_class(index: int, name: str) -> bool:
    return index in MUSIC_INDEX_RANGE or name in MUSIC_EXTRA_CLASSES


def _group_scores(names: Sequence[str], scores: Sequence[float]) -> dict[SegmentLabel, float]:
    """Best class score of each mapped group; unmapped classes can't win."""
    out: dict[SegmentLabel, float] = {"music": 0.0, "speech": 0.0, "silence": 0.0}
    for i, name in enumerate(names):
        if _is_music_class(i, name):
            out["music"] = max(out["music"], scores[i])
        elif name in SPEECH_CLASSES:
            out["speech"] = max(out["speech"], scores[i])
        elif name in SILENCE_CLASSES:
            out["silence"] = max(out["silence"], scores[i])
    return out


def frame_label(names: Sequence[str], scores: Sequence[float], noise_score: float) -> SegmentLabel:
    groups = _group_scores(names, scores)
    best = max(groups, key=lambda g: groups[g])
    return best if groups[best] >= noise_score else "noise"


def _majority(labels: list[SegmentLabel], width: int) -> list[SegmentLabel]:
    if width <= 1 or len(labels) < 2:
        return labels
    half = width // 2
    out = []
    for i, label in enumerate(labels):
        window = labels[max(0, i - half) : i + half + 1]
        counts = Counter(window)
        top = counts.most_common()
        out.append(label if counts[label] == top[0][1] else top[0][0])
    return out


def frame_labels(
    audio: Path,
    duration_ms: int,
    model: YamnetModel,
    options: YamnetOptions,
) -> list[SegmentLabel]:
    """One label per YAMNet hop covering [0, duration_ms), majority-filtered."""
    labels: list[SegmentLabel] = []
    chunk_ms = options.chunk_s * 1000
    for start_ms in range(0, duration_ms, chunk_ms):
        end_ms = min(start_ms + chunk_ms, duration_ms)
        decode_end_ms = min(end_ms + round(options.overlap_s * 1000), duration_ms)
        samples = _decode(audio, start_ms / 1000, decode_end_ms / 1000)
        if not samples:
            continue
        try:
            scores = model(samples)
        except Exception as exc:
            raise ModuleError(f"YAMNet failed on {audio}: {exc}") from exc
        keep = round((end_ms - start_ms) / HOP_MS)
        for row in scores[:keep]:
            labels.append(frame_label(model.class_names, row, options.noise_score))
    return _majority(labels, options.majority_frames)
