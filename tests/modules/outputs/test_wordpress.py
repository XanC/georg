# SPDX-License-Identifier: AGPL-3.0-only
import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from georg.core.config import OutputConfig
from georg.core.contracts import OutputTarget
from georg.core.errors import ConfigError, ModuleError
from georg.core.types import ArtifactRef, PublicationRequest, SermonMetadata
from georg.modules.outputs.wordpress import (
    PUBLICATION_KEY_PATTERN,
    WordPressOutput,
    normalize_scripture,
    slugify,
    transcript_html,
)
from georg.modules.outputs.wordpress_signing import canonical_string
from georg.pipeline.registry import build_output

API = "https://church.example/wp-json/georg/v1"
KEY = "georg:stpaul:service:7:website"
SECRET = bytes(range(100, 132))
SECRET_TEXT = base64.urlsafe_b64encode(SECRET).rstrip(b"=").decode()
KEY_ID = "2026-11"


@dataclass
class FakeBridge:
    """In-memory georg-bridge: attachments and sermons keyed by publication key."""

    attachments: dict[str, dict[str, Any]] = field(default_factory=dict)
    sermons: dict[str, dict[str, Any]] = field(default_factory=dict)
    terms: dict[str, set[str]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    nonces: set[str] = field(default_factory=set)
    next_id: int = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._verified(request):
            return httpx.Response(401, json={"code": "georg_invalid_signature", "message": "no"})
        path = request.url.path.removeprefix("/wp-json/georg/v1")
        if path == "/audio" and request.method == "POST":
            return self._audio(request)
        if path.startswith("/sermons/"):
            key = path.removeprefix("/sermons/")
            if request.method == "GET":
                if key not in self.sermons:
                    return httpx.Response(404, json={"code": "georg_not_found", "message": "x"})
                return httpx.Response(200, json=self.sermons[key])
            if request.method == "PUT":
                return self._put(key, json.loads(request.content))
        return httpx.Response(404, json={"code": "rest_no_route", "message": "No route"})

    def _verified(self, request: httpx.Request) -> bool:
        h = request.headers
        if h.get("x-georg-key-id") != KEY_ID or h.get("x-georg-nonce") in self.nonces:
            return False
        body_sha256 = hashlib.sha256(request.read()).hexdigest()
        if h.get("x-georg-content-sha256") != body_sha256:
            return False
        message = canonical_string(
            key_id=KEY_ID,
            timestamp=int(h["x-georg-timestamp"]),
            nonce=h["x-georg-nonce"],
            method=request.method,
            route=request.url.path.removeprefix("/wp-json"),
            query=dict(request.url.params),
            body_sha256=body_sha256,
        )
        expected = hmac.new(SECRET, message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, h.get("x-georg-signature", "")):
            return False
        self.nonces.add(h["x-georg-nonce"])
        return True

    def _audio(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "audio/mpeg"
        key = request.url.params["publication_key"]
        digest = hashlib.sha256(request.read()).hexdigest()
        existing = self.attachments.get(key)
        if existing and existing["sha256"] == digest:
            return httpx.Response(200, json={**existing, "created": False})
        att_id = existing["id"] if existing else self._id()
        filename = request.url.params["filename"]
        self.attachments[key] = {
            "id": att_id,
            "url": f"https://church.example/wp-content/uploads/sermons/{filename}",
            "sha256": digest,
        }
        return httpx.Response(201, json={**self.attachments[key], "created": True})

    def _put(self, key: str, body: dict[str, Any]) -> httpx.Response:
        for taxonomy, names in body.get("terms", {}).items():
            self.terms.setdefault(taxonomy, set()).update(names)
        created = key not in self.sermons
        post_id = self._id() if created else self.sermons[key]["id"]
        self.sermons[key] = {
            **body,
            "id": post_id,
            "link": f"https://church.example/?p={post_id}",
            "status": body["status"],
        }
        return httpx.Response(201 if created else 200, json=self.sermons[key])

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id


def make_output(
    handler: Any, monkeypatch: pytest.MonkeyPatch, **options: Any
) -> tuple[WordPressOutput, list[float]]:
    monkeypatch.setenv("GEORG_WP_KEY", SECRET_TEXT)
    config = OutputConfig(
        name="website",
        type="wordpress",
        renditions=("sermon",),
        transcript=True,
        options={
            "base_url": "https://church.example/",
            "key_id": KEY_ID,
            "key": "env:GEORG_WP_KEY",
            **options,
        },
    )
    delays: list[float] = []
    out = WordPressOutput(
        config,
        default_timezone="America/Chicago",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=delays.append,
    )
    return out, delays


def make_request(tmp_path: Path, audio: bytes = b"ID3 fake mp3", **meta: Any) -> PublicationRequest:
    mp3 = tmp_path / "sermon.mp3"
    mp3.write_bytes(audio)
    metadata = {
        "title": "Great Expectations",
        "preacher": "Rev. Richard Mittwede",
        "scripture": ("Luke 12:35-40",),
        "occasion": "All Saints' Day",
        "service_time": "10:30",
        **meta,
    }
    return PublicationRequest(
        publication_key=KEY,
        service_date=date(2026, 11, 1),
        metadata=SermonMetadata(**metadata),
        transcript_markdown="Grace to you & peace.\n\nWe read <Luke 12>\nand pray.",
        files={
            "sermon": ArtifactRef(
                path=mp3,
                sha256=hashlib.sha256(audio).hexdigest(),
                size=len(audio),
                mime="audio/mpeg",
            )
        },
    )


def test_implements_output_target(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    assert isinstance(out, OutputTarget)
    assert out.renditions == frozenset({"sermon"})
    assert out.wants_transcript


def test_create_then_update_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    assert out.find_existing(KEY) is None

    first = out.publish(make_request(tmp_path))
    assert first.target == "website"
    assert first.url == f"https://church.example/?p={first.external_id}"
    assert out.find_existing(KEY) == first

    second = out.publish(make_request(tmp_path, title="Greater Expectations"))
    assert second == first
    assert len(bridge.sermons) == 1
    assert len(bridge.attachments) == 1
    assert bridge.sermons[KEY]["title"] == "Greater Expectations"
    assert bridge.sermons[KEY]["audio_attachment_id"] == bridge.attachments[KEY]["id"]
    methods = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in bridge.requests]
    assert methods == [
        ("GET", KEY),
        ("POST", "audio"),
        ("PUT", KEY),
        ("GET", KEY),
        ("POST", "audio"),
        ("PUT", KEY),
    ]


def test_upload_sends_key_and_templated_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch, filename="stpaul_sermon_{date}_{slug}.mp3")
    out.publish(make_request(tmp_path, audio=b"ID3 audio bytes"))
    upload = bridge.requests[0]
    assert upload.url.params["publication_key"] == KEY
    assert upload.url.params["filename"] == "stpaul_sermon_2026-11-01_great-expectations.mp3"
    assert upload.url.params["title"] == "Great Expectations"
    assert upload.read() == b"ID3 audio bytes"
    assert upload.headers["content-length"] == str(len(b"ID3 audio bytes"))
    assert "transfer-encoding" not in upload.headers


def test_defaults_to_draft_and_maps_terms_and_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    out.publish(make_request(tmp_path))
    body = bridge.sermons[KEY]
    assert body["status"] == "draft"
    assert body["terms"] == {
        "ctc_sermon_speaker": ["Rev. Richard Mittwede"],
        "ctc_sermon_book": ["Luke 12:35-40"],
        "ctc_sermon_series": ["All Saints' Day"],
    }
    assert body["date"] == "2026-11-01T10:30:00"
    assert body["date_gmt"] == "2026-11-01T16:30:00"


def test_status_and_custom_term_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    out, _ = make_output(
        bridge,
        monkeypatch,
        status="publish",
        service_time="18:00",
        term_fields={
            "preacher": "ctc_sermon_speaker",
            "series": "ctc_sermon_series",
            "occasion": "ctc_sermon_series",
            "topics": "ctc_sermon_topic",
        },
    )
    request = make_request(
        tmp_path,
        preacher=None,
        service_time=None,
        series="Advent",
        extra={"topics": ["Hope", " ", "Hope", "Waiting"]},
    )
    body = out.sermon_body(request, audio_id=None)
    assert body["status"] == "publish"
    assert body["date"] == "2026-11-01T18:00:00"
    assert body["terms"] == {
        "ctc_sermon_speaker": [],
        "ctc_sermon_series": ["Advent", "All Saints' Day"],
        "ctc_sermon_topic": ["Hope", "Waiting"],
    }
    assert "audio_attachment_id" not in body


def test_scripture_terms_not_joined_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    request = make_request(tmp_path, scripture=("Isaiah 6:1-7", "John 3:1-17"))
    terms = out.sermon_body(request, audio_id=None)["terms"]
    assert terms["ctc_sermon_book"] == ["Isaiah 6:1-7", "John 3:1-17"]


def test_join_terms_makes_one_scripture_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch, join_terms={"scripture": ", "})
    request = make_request(
        tmp_path, scripture=("Isaiah 6:1\u20137", "John 3:1 \u2014 17", "Isaiah 6:1-7")
    )
    terms = out.sermon_body(request, audio_id=None)["terms"]
    assert terms["ctc_sermon_book"] == ["Isaiah 6:1-7, John 3:1-17"]
    assert terms["ctc_sermon_speaker"] == ["Rev. Richard Mittwede"]
    empty = out.sermon_body(make_request(tmp_path, scripture=()), audio_id=None)["terms"]
    assert empty["ctc_sermon_book"] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Luke 12:35-40", "Luke 12:35-40"),
        ("Luke 12:35\u201340", "Luke 12:35-40"),
        ("Luke 12:35 \u2014 40", "Luke 12:35-40"),
        ("Luke 12:35\u20122:1", "Luke 12:35-2:1"),
        ("Luke 12:35 \u2212 40", "Luke 12:35-40"),
        ("Luke 12:35\u201140", "Luke 12:35-40"),
        ("Luke 12:35 - 40", "Luke 12:35-40"),
    ],
)
def test_normalize_scripture(raw: str, expected: str) -> None:
    assert normalize_scripture(raw) == expected


def test_content_is_escaped_paragraphs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch, content_prefix="[Machine transcription]")
    body = out.sermon_body(make_request(tmp_path), audio_id=5)
    assert body["content"] == (
        "<p>[Machine transcription]</p>\n\n"
        "<p>Grace to you &amp; peace.</p>\n\n"
        "<p>We read &lt;Luke 12&gt; and pray.</p>"
    )
    assert body["audio_attachment_id"] == 5


def test_no_content_without_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    request = make_request(tmp_path).model_copy(update={"transcript_markdown": None})
    assert "content" not in out.sermon_body(request, audio_id=None)


def test_title_falls_back_to_occasion_then_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    assert out.sermon_body(make_request(tmp_path, title=None), audio_id=None)["title"] == (
        "All Saints' Day"
    )
    request = make_request(tmp_path, title=" ", occasion=None)
    assert out.sermon_body(request, audio_id=None)["title"] == "Sermon, 2026-11-01"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_errors_are_retried_then_retryable(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"code": "busy", "message": "try later"})

    out, delays = make_output(handler, monkeypatch, attempts=3)
    with pytest.raises(ModuleError) as info:
        out.find_existing(KEY)
    assert info.value.retryable
    assert len(calls) == 3
    assert len(delays) == 2


def test_retry_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    failures = [httpx.Response(502, text="Bad gateway")]

    def handler(request: httpx.Request) -> httpx.Response:
        return failures.pop() if failures else bridge(request)

    out, _ = make_output(handler, monkeypatch)
    assert out.publish(make_request(tmp_path)).external_id == str(bridge.sermons[KEY]["id"])


def test_network_errors_are_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    out, _ = make_output(handler, monkeypatch, attempts=2)
    with pytest.raises(ModuleError) as info:
        out.find_existing(KEY)
    assert info.value.retryable


def test_client_errors_are_not_retryable_and_hide_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            403, json={"code": "georg_cannot_publish", "message": "This user cannot publish."}
        )

    out, delays = make_output(handler, monkeypatch)
    with pytest.raises(ModuleError) as info:
        out.publish(make_request(tmp_path))
    assert not info.value.retryable
    assert "HTTP 403" in str(info.value)
    assert "This user cannot publish." in str(info.value)
    assert SECRET_TEXT not in str(info.value)
    assert len(calls) == 1
    assert delays == []


def test_upload_limit_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    message = "The file is larger than PHP upload_max_filesize (8M)."

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(413, json={"code": "georg_too_large", "message": message})

    out, _ = make_output(handler, monkeypatch)
    with pytest.raises(ModuleError) as info:
        out.publish(make_request(tmp_path))
    assert not info.value.retryable
    assert "HTTP 413" in str(info.value)
    assert message in str(info.value)
    assert len(calls) == 1


def test_wrong_key_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    monkeypatch.setenv("OTHER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    out, _ = make_output(bridge, monkeypatch, key="env:OTHER_KEY")
    with pytest.raises(ModuleError) as info:
        out.find_existing(KEY)
    assert not info.value.retryable
    assert "HTTP 401" in str(info.value)
    assert len(bridge.requests) == 1


def test_missing_rendition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    request = make_request(tmp_path).model_copy(update={"files": {}})
    with pytest.raises(ModuleError, match="no 'sermon' rendition"):
        out.publish(request)


def test_rejects_unsafe_publication_key(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    with pytest.raises(ModuleError):
        out.find_existing("../wp/v2/users")


@pytest.mark.parametrize(
    "options",
    [
        {"status": "pending"},
        {"join_terms": {"topics": ", "}},
        {"join_terms": {"scripture": ""}},
        {"filename": "{date}/{slug}.mp3"},
        {"filename": "{title}.mp3"},
        {"filename": "sermon.wav"},
        {"service_time": "25:00"},
        {"timezone": "Mars/Olympus"},
        {"unknown": 1},
        {"username": "georg"},
        {"app_password": "x"},
        {"key_id": "bad id"},
        {"base_url": "http://church.example"},
        {"base_url": "ftp://church.example", "allow_insecure_http": True},
    ],
)
def test_invalid_options(options: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError):
        make_output(FakeBridge(), monkeypatch, **options)


def test_missing_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEORG_WP_KEY", raising=False)
    config = OutputConfig(
        name="website",
        type="wordpress",
        options={"base_url": "https://x", "key_id": "k", "key": "env:GEORG_WP_KEY"},
    )
    with pytest.raises(ConfigError, match="GEORG_WP_KEY"):
        WordPressOutput(config)


@pytest.mark.parametrize(
    "secret", [base64.urlsafe_b64encode(b"s" * 31).decode(), "not base64!", "c2hvcnQ"]
)
def test_weak_or_malformed_key_is_rejected_without_echo(
    secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAD_KEY", secret)
    with pytest.raises(ConfigError) as info:
        make_output(FakeBridge(), monkeypatch, key="env:BAD_KEY")
    assert secret not in str(info.value)


def test_http_base_url_needs_explicit_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "http"
        return httpx.Response(404, json={"code": "georg_not_found", "message": "x"})

    out, _ = make_output(
        handler, monkeypatch, base_url="http://localhost:8080", allow_insecure_http=True
    )
    assert out.find_existing(KEY) is None


def test_retries_are_signed_again_with_fresh_nonces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = FakeBridge()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) in (1, 3):
            bridge(request)
            raise httpx.ReadTimeout("response lost", request=request)
        return bridge(request)

    out, delays = make_output(handler, monkeypatch, attempts=2)
    ref = out.publish(make_request(tmp_path))
    nonces = [r.headers["x-georg-nonce"] for r in seen]
    assert len(seen) == 4
    assert len(set(nonces)) == 4
    assert all(len(n) == 32 and int(n, 16) >= 0 for n in nonces)
    assert seen[0].read() == seen[1].read() == b"ID3 fake mp3"
    assert len(bridge.attachments) == 1
    assert len(bridge.sermons) == 1
    assert ref.external_id == str(bridge.sermons[KEY]["id"])
    assert len(delays) == 2


def test_key_never_logged_or_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "georg_invalid_signature", "message": "no"})

    caplog.set_level(logging.DEBUG)
    out, _ = make_output(handler, monkeypatch)
    with pytest.raises(ModuleError) as info:
        out.publish(make_request(tmp_path))
    texts = [str(info.value), repr(info.value), repr(out.__dict__), caplog.text]
    for text in texts:
        assert SECRET_TEXT not in text
        assert SECRET.hex() not in text
        assert repr(SECRET) not in text


def test_rendition_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEORG_WP_KEY", SECRET_TEXT)
    config = OutputConfig(
        name="website",
        type="wordpress",
        renditions=("service_hq",),
        options={"base_url": "https://x", "key_id": "k", "key": "env:GEORG_WP_KEY"},
    )
    with pytest.raises(ConfigError, match="rendition"):
        WordPressOutput(config)


def test_helpers() -> None:
    assert slugify("All Saints' Day: “Blessed”!") == "all-saints-day-blessed"
    assert slugify("!!!") == "sermon"
    assert transcript_html("  \n\n") == ""
    assert transcript_html('a "quoted" <b>') == '<p>a "quoted" &lt;b&gt;</p>'


def test_registered_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEORG_WP_KEY", SECRET_TEXT)
    config = OutputConfig(
        name="website",
        type="wordpress",
        options={
            "base_url": "https://church.example",
            "key_id": KEY_ID,
            "key": "env:GEORG_WP_KEY",
        },
    )
    out = build_output(config, timezone="America/Chicago")
    assert isinstance(out, WordPressOutput)
    assert out.sermon_body(make_request(tmp_path), audio_id=None)["date_gmt"].startswith(
        "2026-11-01T16:30"
    )
    sftp = OutputConfig(
        name="phone",
        type="sftp",
        options={"host": "h", "username": "u", "password": "p", "remote_path": "/sermon.mp3"},
    )
    assert build_output(sftp, timezone="America/Chicago").name == "phone"


def test_pipeline_publication_keys_are_accepted() -> None:
    assert PUBLICATION_KEY_PATTERN.match("georg:stpaul:sermon-123:website")
    assert PUBLICATION_KEY_PATTERN.match("georg:test-2:sermon-1:my_site.v2")
