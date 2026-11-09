# SPDX-License-Identifier: AGPL-3.0-only
"""A `FolderStore` backed by the Drive v3 API."""

from collections.abc import Iterable

from georg.gdrive.client import FOLDER_MIME, DriveClient, quote_query
from georg.service_folders import Folder


class DriveFolderStore:
    """Lists and creates Drive folders for `ServiceFolderLayout`."""

    def __init__(self, drive: DriveClient, *, all_drives: bool = True) -> None:
        self._drive = drive
        self._all_drives = all_drives

    def list_folders(self, parent: str) -> Iterable[Folder]:
        query = (
            f"'{quote_query(parent)}' in parents and trashed = false and mimeType = '{FOLDER_MIME}'"
        )
        for raw in self._drive.list_files(
            query, "id, name", all_drives=self._all_drives, order_by="createdTime"
        ):
            yield Folder(ref=str(raw["id"]), name=str(raw.get("name", "")), raw=raw)

    def create_folder(self, name: str, parent: str) -> str:
        return str(self._drive.create_folder(name, parent)["id"])
