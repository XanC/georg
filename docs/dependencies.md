# Dependencies and license notes

| Dependency | Use | License | Notes |
|---|---|---|---|
| FFmpeg | Audio/video processing, invoked as a subprocess | LGPL/GPL (distribution build) | The container image ships Debian's FFmpeg package; its source is available from Debian. Georg does not link to FFmpeg. |
| httpx | HTTP client for the transcription and chat APIs (`georg.llm`) | BSD-3-Clause | Pulls in httpcore (BSD), h11 (MIT), anyio (MIT), idna (BSD), certifi (MPL-2.0, unmodified). |

Runtime Python dependencies are checked against an allow-list in CI (`scripts/check_licenses.py`).
