# Dependencies and license notes

| Dependency | Use | License | Notes |
|---|---|---|---|
| SQLAlchemy | ORM and SQL toolkit for the SQLite database | MIT | |
| Alembic | Database schema migrations | MIT | Pulls in Mako (MIT) and MarkupSafe (BSD-3-Clause). |
| FFmpeg | Audio/video processing, invoked as a subprocess | LGPL/GPL (distribution build) | The container image ships Debian's FFmpeg package; its source is available from Debian. Georg does not link to FFmpeg. |
| paramiko | SFTP client for the `sftp` file-drop output | LGPL-2.1 | Used unmodified as a library, which LGPL permits from AGPL code; pulls in `cryptography` (Apache-2.0/BSD), `bcrypt` (Apache-2.0), `PyNaCl` (Apache-2.0), `invoke` (BSD). `LGPL` is on the allow-list in `scripts/check_licenses.py`. |
| httpx | HTTP client for the transcription and chat APIs (`georg.llm`) | BSD-3-Clause | Pulls in httpcore (BSD), h11 (MIT), anyio (MIT), idna (BSD), certifi (MPL-2.0, unmodified). |

Runtime Python dependencies are checked against an allow-list in CI (`scripts/check_licenses.py`).
