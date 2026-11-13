# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory Google Drive v3 server for `httpx.MockTransport`, shared by the Drive tests.

It understands only the requests Georg sends: listing with the simple `q` clauses the
modules use, metadata gets, `alt=media` downloads, folder creation, and resumable
uploads.
"""

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

FOLDER_MIME = "application/vnd.google-apps.folder"
SESSION_PREFIX = "https://www.googleapis.com/upload/session/"


def rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class FakeFile:
    id: str
    name: str
    mime: str
    parents: list[str]
    content: bytes = b""
    created: datetime = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    modified: datetime = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    app_properties: dict[str, str] = field(default_factory=dict)
    trashed: bool = False
    has_size: bool = True
    reported_md5: str | None = None
    can_add_children: bool = True

    def resource(self, fields: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "mimeType": self.mime,
            "createdTime": rfc3339(self.created),
            "modifiedTime": rfc3339(self.modified),
            "trashed": self.trashed,
            "webViewLink": f"https://drive.google.com/file/d/{self.id}/view",
            "appProperties": dict(self.app_properties),
            "capabilities": {"canAddChildren": self.can_add_children, "canListChildren": True},
        }
        if self.mime != FOLDER_MIME and self.has_size:
            data["size"] = str(len(self.content))
            data["md5Checksum"] = self.reported_md5 or hashlib.md5(self.content).hexdigest()
        wanted = {f.strip() for f in fields.split(",")} if fields else set(data)
        return {k: v for k, v in data.items() if k in wanted}


@dataclass
class Session:
    file_id: str | None
    metadata: dict[str, Any]
    total: int
    fields: str
    received: bytearray = field(default_factory=bytearray)


@dataclass
class FakeDrive:
    files: dict[str, FakeFile] = field(default_factory=dict)
    page_size: int = 2
    failures: list[tuple[str, int]] = field(default_factory=list)
    """(path regex, status) responses returned once each before normal handling."""
    corrupt_downloads: bool = False
    drop_chunk_once: bool = False
    """Accept only half of the first chunk, as if the connection broke mid-upload."""
    requests: list[httpx.Request] = field(default_factory=list)
    sessions: dict[str, Session] = field(default_factory=dict)
    _ids: "itertools.count[int]" = field(default_factory=lambda: itertools.count(1))

    def add(self, name: str, parent: str, content: bytes = b"", **kw: Any) -> FakeFile:
        file_id = kw.pop("id", None) or f"f{next(self._ids):03d}"
        mime = kw.pop("mime", "video/mp4")
        item = FakeFile(id=file_id, name=name, mime=mime, parents=[parent], content=content, **kw)
        self.files[file_id] = item
        return item

    def folder(self, name: str, parent: str = "root", **kw: Any) -> FakeFile:
        return self.add(name, parent, mime=FOLDER_MIME, **kw)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def calls(self, method: str, path_part: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and path_part in r.url.path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for i, (pattern, status) in enumerate(self.failures):
            if re.search(pattern, url):
                del self.failures[i]
                body = {"error": {"code": status, "message": "injected", "errors": []}}
                if status == 403:
                    body["error"]["errors"] = [{"reason": "rateLimitExceeded"}]
                return httpx.Response(status, json=body)
        if url.startswith(SESSION_PREFIX):
            return self._session_put(request)
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": {"code": 401, "message": "no token"}})
        path = request.url.path
        params = request.url.params
        if path.startswith("/upload/drive/v3/files"):
            return self._start_upload(request, path.removeprefix("/upload/drive/v3/files"))
        if path == "/drive/v3/files" and request.method == "GET":
            return self._list(params)
        if path == "/drive/v3/files" and request.method == "POST":
            meta = json.loads(request.content)
            created = self.add(meta["name"], meta["parents"][0], mime=meta["mimeType"])
            return httpx.Response(200, json=created.resource(params.get("fields", "")))
        match = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
        if match and request.method == "GET":
            item = self.files.get(match[1])
            if item is None:
                return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})
            if params.get("alt") == "media":
                data = item.content[:-1] if self.corrupt_downloads else item.content
                return httpx.Response(200, content=data)
            return httpx.Response(200, json=item.resource(params.get("fields", "")))
        return httpx.Response(400, json={"error": {"code": 400, "message": f"unhandled {url}"}})

    def _matches(self, item: FakeFile, query: str) -> bool:
        for clause in re.split(r" and (?![^{]*\})", query):
            clause = clause.strip()
            if m := re.fullmatch(r"'(.*)' in parents", clause):
                ok = m[1] in item.parents
            elif clause == "trashed = false":
                ok = not item.trashed
            elif m := re.fullmatch(r"mimeType = '(.*)'", clause):
                ok = item.mime == m[1]
            elif m := re.fullmatch(r"name = '(.*)'", clause):
                ok = item.name == m[1].replace("\\'", "'").replace("\\\\", "\\")
            elif m := re.fullmatch(r"appProperties has \{ key='(.*)' and value='(.*)' \}", clause):
                value = m[2].replace("\\'", "'").replace("\\\\", "\\")
                ok = item.app_properties.get(m[1]) == value
            else:
                raise AssertionError(f"unsupported query clause {clause!r}")
            if not ok:
                return False
        return True

    def _list(self, params: httpx.QueryParams) -> httpx.Response:
        found = [f for f in self.files.values() if self._matches(f, params["q"])]
        if params.get("orderBy") == "createdTime":
            found.sort(key=lambda f: f.created)
        start = int(params.get("pageToken", "0"))
        page = found[start : start + self.page_size]
        fields = re.fullmatch(r"nextPageToken, files\((.*)\)", params["fields"])
        assert fields is not None
        body: dict[str, Any] = {"files": [f.resource(fields[1]) for f in page]}
        if start + self.page_size < len(found):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)

    def _start_upload(self, request: httpx.Request, rest: str) -> httpx.Response:
        assert request.url.params["uploadType"] == "resumable"
        file_id = rest.lstrip("/") or None
        if file_id is not None and file_id not in self.files:
            return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})
        token = f"s{len(self.sessions) + 1}"
        self.sessions[token] = Session(
            file_id=file_id,
            metadata=json.loads(request.content),
            total=int(request.headers["x-upload-content-length"]),
            fields=request.url.params.get("fields", ""),
        )
        return httpx.Response(200, headers={"Location": SESSION_PREFIX + token})

    def _session_put(self, request: httpx.Request) -> httpx.Response:
        session = self.sessions[str(request.url).removeprefix(SESSION_PREFIX)]
        content_range = request.headers["content-range"]
        if m := re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range):
            assert int(m[1]) == len(session.received), "chunk does not continue the upload"
            chunk = request.content
            if self.drop_chunk_once:
                self.drop_chunk_once = False
                session.received += chunk[: len(chunk) // 2]
                return httpx.Response(503, json={"error": {"code": 503, "message": "reset"}})
            session.received += chunk
        else:
            assert content_range == f"bytes */{session.total}"
        if len(session.received) < session.total:
            headers = {"Range": f"bytes=0-{len(session.received) - 1}"} if session.received else {}
            return httpx.Response(308, headers=headers)
        meta = session.metadata
        if session.file_id is None:
            item = self.add(meta["name"], meta["parents"][0], mime=meta["mimeType"])
        else:
            item = self.files[session.file_id]
            item.name = meta.get("name", item.name)
        item.content = bytes(session.received)
        item.app_properties.update(meta.get("appProperties", {}))
        return httpx.Response(200, json=item.resource(session.fields))
