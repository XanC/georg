# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal Google Drive v3 REST client shared by the Drive input and output modules.

Authentication uses a service account key or a signed-in Google account through
`google-auth` (the optional `gdrive` extra, imported on first use); requests go through
`httpx`.
"""

from georg.gdrive.auth import (
    SCOPE_DRIVE,
    SCOPE_READONLY,
    GoogleTokens,
    TokenSource,
)
from georg.gdrive.client import (
    FOLDER_MIME,
    DriveClient,
    parse_time,
    quote_query,
)

__all__ = [
    "FOLDER_MIME",
    "SCOPE_DRIVE",
    "SCOPE_READONLY",
    "DriveClient",
    "GoogleTokens",
    "TokenSource",
    "parse_time",
    "quote_query",
]
