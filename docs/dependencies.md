# Dependencies and license notes

| Dependency | Use | License | Notes |
|---|---|---|---|
| FFmpeg | Audio/video processing, invoked as a subprocess | LGPL/GPL (distribution build) | The container image ships Debian's FFmpeg package; its source is available from Debian. Georg does not link to FFmpeg. |

Runtime Python dependencies are checked against an allow-list in CI (`scripts/check_licenses.py`).

## Optional extra `classify`

Installed with `uv sync --extra classify` (or `pip install georg[classify]`). Needed only by the `ina_speech_segmenter` audio classifier, which imports it lazily; the base install, type check, and tests work without it.

| Dependency | Use | License | Notes |
|---|---|---|---|
| inaSpeechSegmenter | Speech/music/noise/silence segmentation (CNNs) | MIT | Model weights (`keras_speech_music_noise_cnn.hdf5` etc., MIT, same project) are downloaded from the project's GitHub releases on first use and cached in `$KERAS_HOME` (default `~/.keras`). Pre-populate that directory for offline hosts. |
| TensorFlow, Keras | Neural-network runtime | Apache-2.0 | CPU only. |
| numpy, scipy, pandas, scikit-image, matplotlib, soundfile, h5py, Pyro4, pytextgrid, pyannote.core, namex, and other transitive packages | inaSpeechSegmenter / TensorFlow dependencies | BSD, MIT, Apache-2.0, PSF, MPL-2.0 | `namex` (Apache-2.0) and `pyannote.core` (MIT) omit license metadata; `scripts/check_licenses.py` lists them after checking their LICENSE files. |

inaSpeechSegmenter declares `tensorflow[and-cuda]` and `onnxruntime-gpu`, which pull in proprietary NVIDIA CUDA wheels. `pyproject.toml` overrides these (`[tool.uv] override-dependencies`) to plain `tensorflow` and no `onnxruntime-gpu`: Georg runs on CPU and never imports the ONNX-based VBx segmenter. Installing with pip instead of uv ignores the override and brings the NVIDIA wheels back.

Check the extra's licenses with `uv run --no-dev --extra classify --with pip-licenses python scripts/check_licenses.py`.

Measured cost (CPU only, 8 vCPU, synthetic 2-hour 16 kHz mono FLAC, default 600 s chunks): about 80 s and 2.4 GB peak RSS. Whole-file processing (`chunk_s = None`) took about 62 s but 9 GB.
