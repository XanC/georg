# SPDX-License-Identifier: AGPL-3.0-only
import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from georg.core.config import OutputConfig
from georg.core.contracts import OutputTarget
from georg.core.errors import ConfigError, ModuleError, PublicationConflictError
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
    edited: dict[str, list[str]] = field(default_factory=dict)
    """Fields edited on the website, by publication key."""
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
            if key.endswith("/status") and request.method == "GET":
                return self._status(key.removesuffix("/status"))
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

    def _status(self, key: str) -> httpx.Response:
        if key not in self.sermons:
            return httpx.Response(404, json={"code": "georg_not_found", "message": "x"})
        return httpx.Response(
            200,
            json={
                "id": self.sermons[key]["id"],
                "link": self.sermons[key]["link"],
                "edited": self.edited.get(key, []),
                "modified_gmt": "2026-11-13T16:55:00+00:00",
                "modified_by": "Owner",
                "version": "0.4.0",
            },
        )

    def _put(self, key: str, body: dict[str, Any]) -> httpx.Response:
        requested = body.get("fields")
        edited = set(self.edited.get(key, []))
        conflicts = edited & set(requested or edited) - set(body.get("overwrite", []))
        if key in self.sermons and conflicts:
            return httpx.Response(409, json={"code": "georg_edited_on_site", "message": "x"})
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


@pytest.mark.parametrize("dash", ["-", "\u2013"])
@pytest.mark.parametrize(
    "raw",
    [
        "Luke 12:35-40",
        "Luke 12:35 - 40",
        "Luke 12:35\u201340",
        "Luke 12:35 \u2014 40",
        "Luke 12:35\u201240",
        "Luke 12:35 \u2212 40",
        "Luke 12:35\u201140",
    ],
)
def test_normalize_scripture(raw: str, dash: str) -> None:
    assert normalize_scripture(raw, dash) == f"Luke 12:35{dash}40"


def test_en_dash_separate_terms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch, scripture_dash="\u2013")
    request = make_request(
        tmp_path, scripture=("Isaiah 6:1-7", "John 3:1 \u2014 17", "Isaiah 6:1\u20137")
    )
    terms = out.sermon_body(request, audio_id=None)["terms"]
    assert terms["ctc_sermon_book"] == ["Isaiah 6:1\u20137", "John 3:1\u201317"]


def test_content_is_escaped_paragraphs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch, content_prefix="[Machine transcription]")
    body = out.sermon_body(make_request(tmp_path), audio_id=5)
    assert body["content"] == (
        "<p>[Machine transcription]</p>\n\n"
        "<p>Grace to you &amp; peace.</p>\n\n"
        "<p>We read &lt;Luke 12&gt; and pray.</p>"
    )
    assert body["audio_attachment_id"] == 5
    assert body["full_text"] is True


def test_full_text_only_with_a_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch, content_prefix="[Machine transcription]")
    blank = make_request(tmp_path).model_copy(update={"transcript_markdown": " \n\n "})
    assert "full_text" not in out.sermon_body(blank, audio_id=None)
    off, _ = make_output(FakeBridge(), monkeypatch, full_text=False)
    assert "full_text" not in off.sermon_body(make_request(tmp_path), audio_id=None)


def test_no_content_without_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(FakeBridge(), monkeypatch)
    request = make_request(tmp_path).model_copy(update={"transcript_markdown": None})
    body = out.sermon_body(request, audio_id=None)
    assert "content" not in body
    assert "full_text" not in body


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
        {"scripture_dash": "~"},
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


def test_sermons_on_reads_core_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        rows = [
            {"id": 8216, "link": "https://church.example/s/", "title": {"rendered": "A &amp; B"}}
        ]
        return httpx.Response(200, json=rows)

    out, _ = make_output(handler, monkeypatch)
    assert out.sermons_on(date(2026, 9, 6)) == [
        {"id": "8216", "title": "A & B", "link": "https://church.example/s/"}
    ]
    (request,) = seen
    assert request.url.path == "/wp-json/wp/v2/ctc_sermon"
    assert request.url.params["after"] == "2026-09-05T23:59:59"
    assert request.url.params["before"] == "2026-09-07T00:00:00"
    assert "X-Georg-Signature" not in request.headers


def test_sermons_on_failure_is_a_module_error(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(lambda request: httpx.Response(503), monkeypatch)
    with pytest.raises(ModuleError, match="lookup by date failed"):
        out.sermons_on(date(2026, 9, 6))


def test_sermons_between_pages_through_the_range(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 1
        rows = [
            {
                "id": page * 1000 + i,
                "link": f"https://church.example/{page}-{i}/",
                "title": {"rendered": "A &amp; B"},
                "date": "2026-09-06T10:30:00",
            }
            for i in range(count)
        ]
        return httpx.Response(200, json=rows, headers={"X-WP-TotalPages": "2"})

    out, _ = make_output(handler, monkeypatch)
    posts = out.sermons_between(date(2026, 8, 30), date(2026, 9, 6))
    assert len(posts) == 101
    assert posts[0] == {
        "id": "1000",
        "title": "A & B",
        "link": "https://church.example/1-0/",
        "date": "2026-09-06",
    }
    assert [r.url.params["page"] for r in seen] == ["1", "2"]
    assert seen[0].url.params["after"] == "2026-08-29T23:59:59"
    assert seen[0].url.params["before"] == "2026-09-07T00:00:00"
    assert "X-Georg-Signature" not in seen[0].headers


def test_sermons_between_failure_is_a_module_error(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(lambda request: httpx.Response(503), monkeypatch)
    with pytest.raises(ModuleError, match="lookup by date failed"):
        out.sermons_between(date(2026, 9, 1), date(2026, 9, 6))


def _republish(tmp_path: Path, first: PublicationRequest, **meta: Any) -> PublicationRequest:
    again = make_request(tmp_path, **meta)
    return again.model_copy(update={"previous": first})


def _puts(bridge: FakeBridge) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in bridge.requests if r.method == "PUT"]


def test_first_publish_sends_every_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    request = make_request(tmp_path)
    assert out.fields_to_send(request) is None
    out.publish(request)
    [put] = _puts(bridge)
    assert "fields" not in put and "audio_attachment_id" in put


def test_republish_sends_only_changed_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    first = make_request(tmp_path)
    out.publish(first)
    uploads = sum(r.url.path.endswith("/audio") for r in bridge.requests)

    same = _republish(tmp_path, first)
    assert out.fields_to_send(same) == frozenset()
    retitled = _republish(tmp_path, first, title="Greater Expectations", preacher="Rev. X")
    assert out.fields_to_send(retitled) == {"title", "terms:ctc_sermon_speaker"}
    out.publish(retitled)
    put = _puts(bridge)[-1]
    assert put["fields"] == ["terms:ctc_sermon_speaker", "title"]
    assert "audio_attachment_id" not in put  # audio unchanged: not uploaded again
    assert sum(r.url.path.endswith("/audio") for r in bridge.requests) == uploads

    moved = _republish(tmp_path, first, service_time="08:00")
    assert out.fields_to_send(moved) == {"date"}
    new_audio = make_request(tmp_path, audio=b"ID3 other").model_copy(update={"previous": first})
    assert out.fields_to_send(new_audio) == {"audio"}
    retext = first.model_copy(update={"transcript_markdown": "Other.", "previous": first})
    assert out.fields_to_send(retext) == {"content", "full_text"}


def test_conflict_names_fields_and_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    first = make_request(tmp_path)
    out.publish(first)
    bridge.edited[KEY] = ["content", "title"]
    retitled = _republish(tmp_path, first, title="Greater Expectations", preacher="Rev. X")
    with pytest.raises(PublicationConflictError) as err:
        out.publish(retitled)
    assert err.value.fields == ["title"]
    assert err.value.modified_by == "Owner" and not err.value.retryable
    assert err.value.modified_at == datetime(2026, 11, 13, 16, 55, tzinfo=UTC)
    assert "Owner" in str(err.value) and "title" in str(err.value)
    assert sum(r.method == "PUT" for r in bridge.requests) == 2  # the 409 is not retried
    assert bridge.sermons[KEY]["title"] == "Great Expectations"


def test_skip_and_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    first = make_request(tmp_path)
    out.publish(first)
    bridge.edited[KEY] = ["title"]
    retitled = _republish(tmp_path, first, title="Greater Expectations", preacher="Rev. X")

    out.publish(retitled.model_copy(update={"skip": frozenset({"title"})}))
    put = _puts(bridge)[-1]
    assert put["fields"] == ["terms:ctc_sermon_speaker"] and "overwrite" not in put
    assert bridge.sermons[KEY]["terms"]["ctc_sermon_speaker"] == ["Rev. X"]

    out.publish(retitled.model_copy(update={"overwrite": frozenset({"title", "date"})}))
    put = _puts(bridge)[-1]
    assert put["overwrite"] == ["title"]  # only fields being sent
    assert bridge.sermons[KEY]["title"] == "Greater Expectations"


def test_site_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bridge = FakeBridge()
    out, _ = make_output(bridge, monkeypatch)
    assert out.site_status(KEY) is None
    out.publish(make_request(tmp_path))
    bridge.edited[KEY] = ["terms:ctc_sermon_book"]
    status = out.site_status(KEY)
    assert status is not None
    assert status["edited"] == ["terms:ctc_sermon_book"] and status["modified_by"] == "Owner"
    assert out.field_label("terms:ctc_sermon_book") == "scripture"
    assert out.field_label("content") == "transcript"
    assert out.field_label("terms:ctc_sermon_topic") == "topic"


def test_check_makes_a_signed_read_only_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"code": "georg_not_found"})

    out, _ = make_output(handler, monkeypatch)
    result = out.check()
    assert result.ok and "accepted a signed read-only request" in result.message
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/wp-json/georg/v1/sermons/georg-connection-check"
    assert "X-Georg-Signature" in request.headers


def test_check_reports_a_rejected_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = make_output(lambda request: httpx.Response(401, json={"code": "bad"}), monkeypatch)
    result = out.check()
    assert not result.ok and "georg-bridge failed" in result.message
    assert SECRET_TEXT not in result.message
