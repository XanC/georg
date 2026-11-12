# SPDX-License-Identifier: AGPL-3.0-only
"""Audio classifier backed by YAMNet (Apache-2.0; Google AudioSet model).

YAMNet scores 521 AudioSet classes over ~0.96 s patches every 0.48 s. This module
maps the per-frame scores onto Georg's four labels:

- ``music``: the AudioSet Music subtree (Musical instrument, Singing, Choir,
  Organ, Hymn, Christian/Gospel music, chant), so congregational singing and the
  organ postlude count as music rather than ina's catch-all "noise".
- ``speech``: Speech, Conversation, Narration/monologue, Chatter, Babbling,
  Crowd, so the talking after a service stays speech-adjacent rather than noise.
- ``silence``: the Silence class, or any frame whose RMS is below
  ``YamnetOptions.silence_rms``.
- ``noise``: everything else.

Each frame is labeled by the group with the largest summed class score (below
``min_group_score`` the frame is noise). Group scores are then box-car smoothed
over ``smooth_window_s`` before labeling, and runs shorter than
``min_segment_s`` are merged into a neighbor, to suppress sub-second flicker.

TensorFlow is an optional extra (`georg[classify]`) imported on first use. The
model is the TF2 SavedModel published as `google/yamnet/1` on tfhub.dev (a
tar.gz of a SavedModel plus `assets/yamnet_class_map.csv`); it is downloaded
once into ``cache_dir`` (default ``$KERAS_HOME/models`` or ``~/.keras/models``).
"""

import csv
import gc
import io
import logging
import os
import subprocess
import tarfile
import urllib.request
from array import array
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from georg.core.errors import ConfigError, ModuleError
from georg.core.types import AudioSegment, SegmentLabel
from georg.media import probe
from georg.media.ffmpeg import FFMPEG, MediaError
from georg.modules.processors.classify.ina import _clear_keras_session
from georg.modules.processors.classify.segments import normalize_segments

log = logging.getLogger(__name__)

TYPE_NAME = "yamnet"
_REVISION = 1

MODEL_URL = "https://tfhub.dev/google/yamnet/1?tf-hub-format=compressed"
"""Official YAMNet SavedModel archive (tfhub.dev serves a tar.gz)."""
_MODEL_DIR = "yamnet-tfhub-1"
_CLASS_MAP = Path("assets") / "yamnet_class_map.csv"

SAMPLE_RATE = 16_000
_PATCH_S = 0.96
"""Length of the audio patch each YAMNet frame scores."""
HOP_MS = 480
"""Step between consecutive YAMNet frames."""

MUSIC_CLASSES = frozenset(
    {
        "Music",
        "Musical instrument",
        "Singing",
        "Choir",
        "Chorus",
        "A capella",
        "Male singing",
        "Female singing",
        "Child singing",
        "Organ",
        "Hammond organ",
        "Electronic organ",
        "Pipe organ",
        "Hymn",
        "Christian music",
        "Gospel music",
        "Chant",
        "Gregorian chant",
        "Mantra",
        "Singing bowl",
    }
)
"""AudioSet `Music` subtree classes that keep worship music labeled music."""

SPEECH_CLASSES = frozenset(
    {
        "Speech",
        "Conversation",
        "Narration, monologue",
        "Chatter",
        "Babbling",
        "Crowd",
        "Speech synthesizer",
        "Shout",
        "Whispering",
    }
)
"""AudioSet classes mapped to `speech` (human voice, including crowd chatter)."""

SILENCE_CLASSES = frozenset({"Silence"})

_GROUPS: tuple[tuple[SegmentLabel, frozenset[str]], ...] = (
    ("music", MUSIC_CLASSES),
    ("speech", SPEECH_CLASSES),
    ("silence", SILENCE_CLASSES),
)


class YamnetOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_dir: str | None = None
    """Model cache; default `$KERAS_HOME/models` or `~/.keras/models`."""
    model_url: str = MODEL_URL
    """URL of the YAMNet SavedModel tar.gz."""
    chunk_s: int = Field(default=600, ge=60)
    """Decode and score in windows of this many seconds to bound memory."""
    overlap_s: float = Field(default=2.0, ge=0.0)
    """Extra audio decoded past each chunk end for context; its frames are dropped."""
    smooth_window_s: float = Field(default=2.4, gt=0.0)
    """Box-car window over group scores before labeling frames (~5 YAMNet hops)."""
    min_segment_s: float = Field(default=4.0, ge=0.0)
    """Runs shorter than this merge into the longer adjacent run."""
    min_group_score: float = Field(default=0.0, ge=0.0)
    """Frames whose best summed group score is below this are labeled noise."""
    silence_rms: float = Field(default=0.001, ge=0.0)
    """Frames whose RMS is below this are labeled silence regardless of scores."""


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


def _download_model(url: str, dest: Path) -> None:
    """Fetch and unpack the SavedModel archive atomically into `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".download")
    try:
        with urllib.request.urlopen(url) as response:
            data = response.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        tmp.rename(dest)
    except Exception as exc:
        raise ModuleError(
            f"could not download the YAMNet model from {url}: {exc}", retryable=True
        ) from exc


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


def _load_model(options: YamnetOptions) -> YamnetModel:
    try:
        import tensorflow as tf
    except ImportError as exc:
        raise ModuleError(
            "tensorflow is not installed; install the 'classify' extra (uv sync --extra classify)"
        ) from exc
    model_dir = _cache_root(options) / _MODEL_DIR
    if not (model_dir / "saved_model.pb").exists():
        _download_model(options.model_url, model_dir)
    try:
        model = tf.saved_model.load(str(model_dir))
        with open(model_dir / _CLASS_MAP, newline="") as f:
            class_names = [row["display_name"] for row in csv.DictReader(f)]
    except OSError as exc:
        raise ModuleError(f"could not load the YAMNet model in {model_dir}: {exc}") from exc
    return _SavedModel(model, class_names)


def _decode(audio: Path, start_s: float, stop_s: float) -> array[float]:
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


def _label_frames(
    names: Sequence[str],
    scores: Sequence[Sequence[float]],
    rms: Sequence[float],
    options: YamnetOptions,
) -> list[SegmentLabel]:
    """Smooth the group scores, then label each frame."""
    width = max(1, round(options.smooth_window_s * 1000 / HOP_MS))
    group_totals = [
        [sum(row[i] for i, n in enumerate(names) if n in classes) for _, classes in _GROUPS]
        for row in scores
    ]
    labels: list[SegmentLabel] = []
    for i in range(len(group_totals)):
        lo, hi = max(0, i - width // 2), min(len(group_totals), i + width // 2 + 1)
        n = hi - lo
        smoothed = [sum(t[g] for t in group_totals[lo:hi]) / n for g in range(len(_GROUPS))]
        g = max(range(len(_GROUPS)), key=lambda j: smoothed[j])
        if rms[i] < options.silence_rms:
            labels.append("silence")
        elif smoothed[g] < options.min_group_score:
            labels.append("noise")
        else:
            labels.append(_GROUPS[g][0])
    return _enforce_min_segment(labels, options.min_segment_s)


def _enforce_min_segment(labels: list[SegmentLabel], min_s: float) -> list[SegmentLabel]:
    """Merge runs shorter than `min_s` into their longer adjacent run."""
    if min_s <= 0:
        return labels
    runs: list[list[Any]] = []
    for label in labels:
        if runs and runs[-1][0] == label:
            runs[-1][1] += 1
        else:
            runs.append([label, 1])
    changed = True
    while changed:
        changed = False
        for i, (_label, length) in enumerate(runs):
            if length * HOP_MS >= min_s * 1000 or len(runs) == 1:
                continue
            left = runs[i - 1][1] if i else -1
            right = runs[i + 1][1] if i + 1 < len(runs) else -1
            into = i - 1 if left >= right else i + 1
            runs[into][1] += length
            del runs[i]
            changed = True
            break
    out: list[SegmentLabel] = []
    for label, length in runs:
        out.extend([label] * length)
    return out


class YamnetClassifier:
    """`AudioClassifier` (type "yamnet") labeling a whole recording with YAMNet."""

    type_name = TYPE_NAME

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        model_factory: Callable[[YamnetOptions], YamnetModel] = _load_model,
    ) -> None:
        try:
            self.options = YamnetOptions.model_validate(options or {})
        except ValidationError as exc:
            raise ConfigError(f"{TYPE_NAME}: {exc}") from exc
        self._factory = model_factory
        self._model: YamnetModel | None = None
        o = self.options
        self.version = (
            f"{TYPE_NAME}-r{_REVISION}-tf{_library_version()}"
            f"-c{o.chunk_s}-o{o.overlap_s}-w{o.smooth_window_s}"
            f"-m{o.min_segment_s}-g{o.min_group_score}-r{o.silence_rms}"
        )

    def classify(self, audio: Path) -> list[AudioSegment]:
        duration_ms = probe(audio).duration_ms
        if self._model is None:
            self._model = self._factory(self.options)
        model = self._model
        raw: list[tuple[SegmentLabel, int, int]] = []
        chunk_ms = self.options.chunk_s * 1000
        for start_ms in range(0, duration_ms, chunk_ms):
            end_ms = min(start_ms + chunk_ms, duration_ms)
            decode_end_ms = min(end_ms + round(self.options.overlap_s * 1000), duration_ms)
            samples = _decode(audio, start_ms / 1000, decode_end_ms / 1000)
            if not samples:
                continue
            try:
                scores = model(samples)
            except Exception as exc:
                raise ModuleError(f"YAMNet failed on {audio}: {exc}") from exc
            keep = round((end_ms - start_ms) / HOP_MS)
            scores = scores[:keep] if len(scores) > keep else scores
            rms = _frame_rms(samples, len(scores))
            labels = _label_frames(model.class_names, scores, rms, self.options)
            raw.extend(
                (label, start_ms + i * HOP_MS, start_ms + (i + 1) * HOP_MS)
                for i, label in enumerate(labels)
            )
        return normalize_segments(raw, duration_ms)

    def release(self) -> None:
        """Free the loaded model and TensorFlow's graph state; `classify` reloads it."""
        if self._model is None:
            return
        self._model = None
        _clear_keras_session()
        gc.collect()


def _frame_rms(samples: Sequence[float], frames: int) -> list[float]:
    """RMS of the patch each frame scores: frame i covers [i*hop, i*hop + patch)."""
    hop = round(SAMPLE_RATE * HOP_MS / 1000)
    patch = round(SAMPLE_RATE * _PATCH_S)
    out = []
    for i in range(frames):
        lo = i * hop
        window = samples[lo : lo + patch] or samples[lo:]
        if not window:
            out.append(0.0)
        else:
            out.append((sum(x * x for x in window) / len(window)) ** 0.5)
    return out
