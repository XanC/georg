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

Generation speed is bound by memory bandwidth, so more threads than physical cores rarely helps. On shared or burstable vCPUs spinning OpenMP workers slow llama.cpp down several times over; the formatter therefore sets `OMP_WAIT_POLICY=PASSIVE` unless the environment already sets it.

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

## Optional extra `llm`

Installed with `uv sync --extra llm` (or `pip install georg[llm]`). Needed only by the `llama_cpp` transcript formatter (the default), which imports it lazily.

| Dependency | Use | License | Notes |
|---|---|---|---|
| llama-cpp-python | Runs GGUF language models on the CPU (bundles llama.cpp, MIT) | MIT | Built from source on install (needs a C/C++ compiler; CMake comes from PyPI), about 2 minutes. |
| huggingface-hub | Downloads the model file on first use | Apache-2.0 | Pulls in filelock (Unlicense), fsspec (BSD), tqdm (MPL-2.0/MIT), hf-xet (Apache-2.0) and others. |
| diskcache, numpy, jinja2, typing-extensions | llama-cpp-python dependencies | Apache-2.0, BSD, BSD, PSF | |

Default model: [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) (Apache-2.0), as the Q4_K_M GGUF conversion from [unsloth/Qwen3-4B-Instruct-2507-GGUF](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF) (Apache-2.0), 2.5 GB. Use only models under OSI-approved licenses (e.g. Apache-2.0 or MIT Qwen3, Mistral or Phi releases); Llama, Gemma and Qwen2.5-3B (Qwen research license) models are not allowed. The Qwen3 instruct-2507 models never emit reasoning; for hybrid Qwen3 models the formatter sends `/no_think` and drops any `<think>` block.

The model is downloaded on first use into `cache_dir` (default: the Hugging Face cache, `$HF_HOME`). To pre-download it, for example on a host or image without network access at run time:

```sh
uv run --extra llm hf download unsloth/Qwen3-4B-Instruct-2507-GGUF \
    Qwen3-4B-Instruct-2507-Q4_K_M.gguf --cache-dir /var/lib/georg/models
```

then set `cache_dir = "/var/lib/georg/models"` and `local_files_only = true`, or download with `--local-dir DIR` and set `model_path = "DIR/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"`. The Docker build arg `GEORG_BAKE_MODEL` (off by default) bakes a model into the image at `/opt/georg/models`:

```sh
docker build -f deploy/Dockerfile --build-arg GEORG_EXTRAS="llm" \
    --build-arg GEORG_BAKE_MODEL="unsloth/Qwen3-4B-Instruct-2507-GGUF Qwen3-4B-Instruct-2507-Q4_K_M.gguf" .
```

with `model_path = "/opt/georg/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"` in the formatter options.

Formatter options (`[processors] formatter = { type = "llama_cpp", options = { ... } }`): `model_repo`, `model_file`, `model_path`, `cache_dir`, `local_files_only`, `threads` (0 = llama.cpp default), `context_size` (4096), `temperature` (0), `retry_temperature` (0.3), `seed` (0), `min_chunk_words` (400), `max_chunk_words` (700), `disable_thinking` (true), `prompt_version` / `prompt_file` (the same templates as `openai_chat`). The transcript is split at sentence ends into chunks; a chunk whose words the model changes is retried once at `retry_temperature`, and if it still changes words its original text is kept unformatted and the review page shows a warning.

Check the extra's licenses with `uv run --no-dev --extra llm --with pip-licenses python scripts/check_licenses.py`.

## Optional extra `gdrive`

Installed with `uv sync --extra gdrive` (or `pip install georg[gdrive]`). Needed only by the `google_drive` input and the `google_drive_upload` output, which import it lazily; Drive API calls themselves use `httpx` against the Drive v3 REST API. See [google-drive.md](google-drive.md) for the service account setup.

| Dependency | Use | License | Notes |
|---|---|---|---|
| google-auth | Signs service-account JWTs and exchanges them for OAuth access tokens | Apache-2.0 | `google-api-python-client` is deliberately not used; the few Drive v3 endpoints Georg needs are called directly. |
| pyasn1, pyasn1-modules | Key parsing for google-auth | BSD-2-Clause | `cryptography` (Apache-2.0/BSD) is already installed via paramiko. |

Check the extra's licenses with `uv run --no-dev --extra gdrive --with pip-licenses python scripts/check_licenses.py`.
