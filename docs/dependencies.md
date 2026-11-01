# Dependencies and license notes

| Dependency | Use | License | Notes |
|---|---|---|---|
| FFmpeg | Audio/video processing, invoked as a subprocess | LGPL/GPL (distribution build) | The container image ships Debian's FFmpeg package; its source is available from Debian. Georg does not link to FFmpeg. |
| paramiko | SFTP client for the `sftp` file-drop output | LGPL-2.1 | Used unmodified as a library, which LGPL permits from AGPL code; pulls in `cryptography` (Apache-2.0/BSD), `bcrypt` (Apache-2.0), `PyNaCl` (Apache-2.0), `invoke` (BSD). `LGPL` is on the allow-list in `scripts/check_licenses.py`. |

Runtime Python dependencies are checked against an allow-list in CI (`scripts/check_licenses.py`).
