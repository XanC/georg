# Dependencies and license notes

| Dependency | Use | License | Notes |
|---|---|---|---|
| SQLAlchemy | ORM and SQL toolkit for the SQLite database | MIT | |
| Alembic | Database schema migrations | MIT | Pulls in Mako (MIT) and MarkupSafe (BSD-3-Clause). |
| FFmpeg | Audio/video processing, invoked as a subprocess | LGPL/GPL (distribution build) | The container image ships Debian's FFmpeg package; its source is available from Debian. Georg does not link to FFmpeg. |

Runtime Python dependencies are checked against an allow-list in CI (`scripts/check_licenses.py`).
