# Dependencies and license notes

| Dependency | Use | License | Notes |
|---|---|---|---|
| SQLAlchemy | ORM and SQL toolkit for the SQLite database | MIT | |
| Alembic | Database schema migrations | MIT | Pulls in Mako (MIT) and MarkupSafe (BSD-3-Clause). |
| FFmpeg | Audio/video processing, invoked as a subprocess | LGPL/GPL (distribution build) | The container image ships Debian's FFmpeg package; its source is available from Debian. Georg does not link to FFmpeg. |
| paramiko | SFTP client for the `sftp` file-drop output | LGPL-2.1 | Used unmodified as a library, which LGPL permits from AGPL code; pulls in `cryptography` (Apache-2.0/BSD), `bcrypt` (Apache-2.0), `PyNaCl` (Apache-2.0), `invoke` (BSD). `LGPL` is on the allow-list in `scripts/check_licenses.py`. |
| FastAPI, Starlette | Web framework for the review UI (`georg.web`) | MIT | Starlette's `FileResponse` serves artifacts with HTTP Range support. Pulls in pydantic (MIT), annotated-doc (MIT), typing-inspection (MIT), and opentelemetry-api (Apache-2.0). |
| Jinja2 | Server-rendered HTML templates | BSD-3-Clause | Pulls in MarkupSafe (BSD-3-Clause). |
| uvicorn | ASGI server for `georg web` | BSD-3-Clause | Pulls in click (BSD-3-Clause). |
| python-multipart | Form parsing for FastAPI | Apache-2.0 | |
| itsdangerous | Signs the session cookie (Starlette `SessionMiddleware`) | BSD-3-Clause | |
| argon2-cffi | Argon2id password hashing for review UI users | MIT | Pulls in argon2-cffi-bindings (MIT, bundles the CC0/Apache-2.0 Argon2 reference implementation) and cffi (MIT). |
| htmx 2.0.4 | Small client-side polling (render status, jobs table) | 0BSD (Zero-Clause BSD) | Vendored unmodified as `src/georg/web/static/htmx.min.js` from <https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js>, so nothing is loaded from a CDN at runtime. 0BSD needs no notice; source: <https://github.com/bigskysoftware/htmx>. |
| httpx | HTTP client for the transcription and chat APIs (`georg.llm`) | BSD-3-Clause | Pulls in httpcore (BSD), h11 (MIT), anyio (MIT), idna (BSD), certifi (MPL-2.0, unmodified). |

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

## Optional extra `whisper`

Installed with `uv sync --extra whisper` (or `pip install georg[whisper]`). Needed only by the `faster_whisper` transcriber, which imports it lazily.

| Dependency | Use | License | Notes |
|---|---|---|---|
| faster-whisper | Runs OpenAI Whisper models locally | MIT | Model weights (OpenAI Whisper, MIT; CTranslate2 conversions on Hugging Face) are downloaded on first use into `download_root` or the Hugging Face cache. Pre-populate it and set `local_files_only` for offline hosts. |
| ctranslate2 | Inference engine | MIT | The PyPI wheels run on CPU without any NVIDIA packages; GPU use would need CUDA/cuDNN installed separately and is not part of this extra. |
| onnxruntime, tokenizers, huggingface-hub, av, numpy, and transitive packages | faster-whisper dependencies | MIT, Apache-2.0, BSD, MPL-2.0 | Georg decodes audio with FFmpeg and passes samples to faster-whisper, so PyAV is installed but unused. |

Check the extra's licenses with `uv run --no-dev --extra whisper --with pip-licenses python scripts/check_licenses.py`.

## Optional extra `gdrive`

Installed with `uv sync --extra gdrive` (or `pip install georg[gdrive]`). Needed only by the `google_drive` input and the `google_drive_upload` output, which import it lazily; Drive API calls themselves use `httpx` against the Drive v3 REST API. See [google-drive.md](google-drive.md) for the service account setup.

| Dependency | Use | License | Notes |
|---|---|---|---|
| google-auth | Signs service-account JWTs and exchanges them for OAuth access tokens | Apache-2.0 | `google-api-python-client` is deliberately not used; the few Drive v3 endpoints Georg needs are called directly. |
| pyasn1, pyasn1-modules | Key parsing for google-auth | BSD-2-Clause | `cryptography` (Apache-2.0/BSD) is already installed via paramiko. |

Check the extra's licenses with `uv run --no-dev --extra gdrive --with pip-licenses python scripts/check_licenses.py`.
