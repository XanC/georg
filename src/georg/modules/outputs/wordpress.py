# SPDX-License-Identifier: AGPL-3.0-only
"""WordPress output for Church Content (`ctc_sermon`) sites, via the `georg-bridge` plugin.

`publish` uploads the sermon audio to `POST /georg/v1/audio` (stored as a media-library
attachment under `uploads/sermons/`), then creates or updates the sermon post with
`PUT /georg/v1/sermons/<publication_key>`. Both calls are idempotent by publication key,
so a retried or repeated publication updates the same attachment and post. A republish
sends only the fields that changed since the last one (`PublicationRequest.previous`); the
bridge refuses fields edited on the website (409) unless they are in `overwrite`, which
becomes a `PublicationConflictError`. Every request is signed with HMAC-SHA256 (see
`wordpress_signing` and docs/wordpress.md). The plugin lives in `wordpress/georg-bridge/`
in this repository.
"""

import html
import json
import re
import string
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from georg.core.checks import CheckResult
from georg.core.config import OutputConfig, resolve_secret
from georg.core.errors import ConfigError, ModuleError, PublicationConflictError
from georg.core.types import ArtifactRef, ExternalRef, PublicationRequest, SermonMetadata
from georg.llm.http import request_json
from georg.modules.outputs.wordpress_signing import (
    HEADER_CONTENT_SHA256,
    KEY_ID_PATTERN,
    FileBody,
    GeorgSigner,
    decode_secret,
    file_sha256,
)

PUBLICATION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9:._-]{1,190}$")
FILENAME_FIELDS = frozenset({"date", "slug"})
METADATA_TERM_FIELDS = frozenset({"preacher", "scripture", "occasion", "series"})
SERMON_LIST_PAGE_SIZE = 100
CHECK_PUBLICATION_KEY = "georg-connection-check"
"""Looked up by `check()`; no sermon has it, so a signed GET answers 404."""
SERMON_LIST_MAX_PAGES = 20
DEFAULT_TERM_FIELDS = {
    "preacher": "ctc_sermon_speaker",
    "scripture": "ctc_sermon_book",
    "occasion": "ctc_sermon_series",
}
_DASH = re.compile("\\s*[-\u2011\u2012\u2013\u2014\u2212]\\s*")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class WordPressOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    """Site root, e.g. "https://www.example.org"."""
    rest_prefix: str = "/wp-json"
    key_id: str = Field(pattern=KEY_ID_PATTERN.pattern)
    """Id of the georg-bridge signing key, as listed in the site's GEORG_BRIDGE_KEYS."""
    key: str
    """The base64url signing key, normally an "env:NAME" reference."""
    allow_insecure_http: bool = False
    """Allow an http:// base_url (local testing only)."""
    status: Literal["draft", "private", "publish"] = "draft"
    full_text: bool = True
    """Tick Church Content's "Full sermon text provided" box when the transcript is the
    post content."""
    rendition: str = "sermon"
    filename: str = "sermon_{date}_{slug}.mp3"
    """Audio file name under `uploads/sermons/`; fields {date} (YYYY-MM-DD) and {slug}."""
    timezone: str | None = None
    """IANA zone for the post date, e.g. "America/Chicago"; defaults to `default_timezone`
    (UTC unless the caller passes the instance timezone)."""
    service_time: str = "00:00"
    """Local "HH:MM" used when the metadata has no service time."""
    term_fields: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TERM_FIELDS))
    """Georg field -> taxonomy. Fields: preacher, scripture, occasion, series, or a key of
    `SermonMetadata.extra` holding a string or list of strings."""
    scripture_dash: Literal["-", "\u2013", "\u2014"] = "-"
    """Dash used in scripture terms; any hyphen or dash in the metadata becomes this."""
    join_terms: dict[str, str] = Field(default_factory=dict)
    """Georg field -> separator: join the field's values into a single term, e.g.
    {"scripture": ", "} gives one "Isaiah 6:1-7, John 3:1-17" term."""
    content_prefix: str | None = None
    """Optional first paragraph of the post, e.g. "[Machine transcription]"."""
    timeout_s: float = Field(default=300.0, gt=0)
    attempts: int = Field(default=3, ge=1)

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        fields = {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
        if not fields <= FILENAME_FIELDS:
            raise ValueError(f"unknown template fields {sorted(fields - FILENAME_FIELDS)}")
        if "/" in value or not value.lower().endswith(".mp3"):
            raise ValueError("filename must be a bare name ending in .mp3")
        return value

    @model_validator(mode="after")
    def _https(self) -> "WordPressOptions":
        scheme = urlsplit(self.base_url).scheme
        if scheme != "https" and not (scheme == "http" and self.allow_insecure_http):
            raise ValueError("base_url must be https:// (allow_insecure_http is for local tests)")
        return self

    @model_validator(mode="after")
    def _join_terms(self) -> "WordPressOptions":
        unknown = set(self.join_terms) - set(self.term_fields)
        if unknown:
            raise ValueError(f"join_terms fields not in term_fields: {sorted(unknown)}")
        if any(not sep for sep in self.join_terms.values()):
            raise ValueError("join_terms separators must not be empty")
        return self

    @field_validator("service_time")
    @classmethod
    def _service_time(cls, value: str) -> str:
        if not _TIME.match(value):
            raise ValueError("service_time must be HH:MM")
        return value


def slugify(text: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "sermon"


def transcript_html(markdown: str, prefix: str | None = None) -> str:
    """Render text as escaped `<p>` paragraphs; blank lines separate paragraphs."""
    blocks = [prefix] if prefix else []
    blocks += re.split(r"\n\s*\n", markdown)
    paragraphs = (" ".join(block.split()) for block in blocks)
    return "\n\n".join(f"<p>{html.escape(p, quote=False)}</p>" for p in paragraphs if p)


def normalize_scripture(reference: str, dash: str = "-") -> str:
    """Replace every hyphen or dash (and the spaces around it) with `dash`."""
    return _DASH.sub(dash, reference)


def _timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _values(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list | tuple) or not all(isinstance(i, str) for i in items):
        raise ModuleError(f"term value must be a string or list of strings, got {raw!r}")
    return list(dict.fromkeys(i.strip() for i in items if i.strip()))


class WordPressOutput:
    """`OutputTarget` of type "wordpress"."""

    def __init__(
        self,
        config: OutputConfig,
        *,
        default_timezone: str = "UTC",
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self.options = WordPressOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ConfigError(f"output {config.name!r}: {exc}") from exc
        opts = self.options
        if config.renditions and set(config.renditions) != {opts.rendition}:
            raise ConfigError(
                f"output {config.name!r} uploads rendition {opts.rendition!r} "
                f"but lists {list(config.renditions)}"
            )
        try:
            self._tz = ZoneInfo(opts.timezone or default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"output {config.name!r}: unknown timezone: {exc}") from exc
        self.name = config.name
        self.renditions = frozenset({opts.rendition})
        self.wants_transcript = config.transcript
        rest_path = urlsplit(opts.base_url).path.rstrip("/") + "/" + opts.rest_prefix.strip("/")
        self._rest = opts.base_url.rstrip("/") + "/" + opts.rest_prefix.strip("/")
        try:
            secret = decode_secret(resolve_secret(opts.key))
        except ValueError as exc:
            raise ConfigError(f"output {config.name!r}: {exc}") from None
        self._auth = GeorgSigner(opts.key_id, secret, rest_path=rest_path)
        self._client = http_client or httpx.Client(timeout=opts.timeout_s)
        self._sleep = sleep

    def find_existing(self, publication_key: str) -> ExternalRef | None:
        data = self._call("GET", self._sermon_route(publication_key), none_on=frozenset({404}))
        return None if data is None else self._ref(data)

    def check(self) -> CheckResult:
        """A signed, read-only georg-bridge call: look up a publication key no sermon has.
        The bridge checks the signature before the lookup, so a 404 proves the key works."""
        try:
            self.find_existing(CHECK_PUBLICATION_KEY)
        except ModuleError as exc:
            return CheckResult(False, f"Signed request to georg-bridge failed: {exc}")
        return CheckResult(True, "georg-bridge accepted a signed read-only request.")

    def sermons_on(self, day: date) -> list[dict[str, Any]]:
        """Published ctc_sermon posts dated `day` (site time): id, title and link.

        Reads the core REST API, which lists published sermons without authentication,
        so posts from before Georg (with no publication key) are found too.
        """
        start = datetime.combine(day, datetime.min.time())
        try:
            response = self._client.get(
                self._rest + "/wp/v2/ctc_sermon",
                params={
                    "after": (start - timedelta(seconds=1)).isoformat(),
                    "before": (start + timedelta(days=1)).isoformat(),
                    "_fields": "id,link,title",
                    "per_page": 20,
                },
                headers={"Accept": "application/json"},
                timeout=min(self.options.timeout_s, 5.0),
            )
            response.raise_for_status()
            rows = response.json()
            return [
                {
                    "id": str(int(row["id"])),
                    "title": html.unescape(str(row["title"]["rendered"])),
                    "link": str(row["link"]),
                }
                for row in rows
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModuleError(f"{self.name}: sermon lookup by date failed: {exc}") from exc

    def sermons_between(self, first: date, last: date) -> list[dict[str, Any]]:
        """Published ctc_sermon posts dated `first`..`last` inclusive (site time): id, title,
        link and date (YYYY-MM-DD). One paginated listing, for many services at once."""
        start = datetime.combine(first, datetime.min.time())
        end = datetime.combine(last, datetime.min.time()) + timedelta(days=1)
        posts: list[dict[str, Any]] = []
        try:
            for page in range(1, SERMON_LIST_MAX_PAGES + 1):
                response = self._client.get(
                    self._rest + "/wp/v2/ctc_sermon",
                    params={
                        "after": (start - timedelta(seconds=1)).isoformat(),
                        "before": end.isoformat(),
                        "_fields": "id,link,title,date",
                        "per_page": SERMON_LIST_PAGE_SIZE,
                        "page": page,
                        "orderby": "date",
                        "order": "desc",
                    },
                    headers={"Accept": "application/json"},
                    timeout=min(self.options.timeout_s, 5.0),
                )
                response.raise_for_status()
                rows = response.json()
                posts += [
                    {
                        "id": str(int(row["id"])),
                        "title": html.unescape(str(row["title"]["rendered"])),
                        "link": str(row["link"]),
                        "date": str(row["date"])[:10],
                    }
                    for row in rows
                ]
                pages = int(response.headers.get("X-WP-TotalPages") or page)
                if page >= pages or len(rows) < SERMON_LIST_PAGE_SIZE:
                    break
            return posts
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModuleError(f"{self.name}: sermon lookup by date failed: {exc}") from exc

    def publish(self, request: PublicationRequest) -> ExternalRef:
        route = self._sermon_route(request.publication_key)
        fields = self.fields_to_send(request)
        artifact = self._artifact(request)
        audio = self._upload(request, artifact) if fields is None or "audio" in fields else None
        body = self.sermon_body(request, audio_id=audio)
        overwrite = set(request.overwrite)
        if fields is not None:
            body["fields"] = sorted(fields)
            overwrite &= fields
        if overwrite:
            body["overwrite"] = sorted(overwrite)
        data = self._call(
            "PUT",
            route,
            content=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            none_on=frozenset({409}),
        )
        if data is None:
            raise self._conflict(request, fields)
        return self._ref(data)

    def fields_to_send(self, request: PublicationRequest) -> frozenset[str] | None:
        """Bridge fields that differ from `request.previous`, minus `request.skip`; None
        (send everything) for a first publish."""
        now = self._comparable(request)
        if request.previous is None:
            return frozenset(now) - request.skip if request.skip else None
        before = self._comparable(request.previous)
        changed = {f for f, v in now.items() if v != before.get(f)}
        return frozenset(changed) - request.skip

    def _comparable(self, request: PublicationRequest) -> dict[str, Any]:
        body = self.sermon_body(request, audio_id=None)
        values: dict[str, Any] = {"title": body["title"], "date": (body["date"], body["date_gmt"])}
        if "content" in body:
            values["content"] = body["content"]
            values["full_text"] = (body["content"], body.get("full_text", False))
        values |= {f"terms:{t}": sorted(names) for t, names in body["terms"].items()}
        artifact = request.files.get(self.options.rendition)
        values["audio"] = artifact.sha256 if artifact else None
        return values

    def site_status(self, publication_key: str) -> dict[str, Any] | None:
        """The bridge's view of the published sermon: id, link, `edited` (fields changed on
        the website since Georg wrote them), `modified_at` and `modified_by`; None if the
        website has no sermon with this key."""
        data = self._call(
            "GET", self._sermon_route(publication_key) + "/status", none_on=frozenset({404})
        )
        if data is None:
            return None
        edited = data.get("edited")
        if not isinstance(edited, list) or not all(isinstance(f, str) for f in edited):
            raise ModuleError(f"{self.name}: bridge status has no edited fields")
        by = data.get("modified_by")
        return {
            "id": data.get("id"),
            "link": data.get("link"),
            "edited": edited,
            "modified_at": _timestamp(data.get("modified_gmt")),
            "modified_by": by if isinstance(by, str) else None,
            "version": data.get("version"),
        }

    def field_label(self, field: str) -> str:
        """Reviewer-facing name of a bridge field, e.g. "terms:ctc_sermon_book" -> "scripture"."""
        if field.startswith("terms:"):
            taxonomy = field.removeprefix("terms:")
            names = [f for f, t in self.options.term_fields.items() if t == taxonomy]
            return " and ".join(names) if names else taxonomy.removeprefix("ctc_sermon_")
        return {"content": "transcript", "full_text": "full-text flag"}.get(field, field)

    def _conflict(
        self, request: PublicationRequest, fields: frozenset[str] | None
    ) -> PublicationConflictError:
        status = self.site_status(request.publication_key) or {}
        edited = set(status.get("edited") or [])
        conflicting = edited if fields is None else edited & fields
        conflicting -= request.overwrite
        names = sorted(conflicting or edited)
        by = status.get("modified_by")
        return PublicationConflictError(
            f"{self.name}: edited on the website"
            + (f" by {by}" if by else "")
            + f": {', '.join(self.field_label(f) for f in names) or 'unknown fields'}",
            fields=names,
            modified_by=by,
            modified_at=status.get("modified_at"),
        )

    def sermon_body(self, request: PublicationRequest, *, audio_id: int | None) -> dict[str, Any]:
        meta = request.metadata
        local = datetime.combine(
            meta.service_date or request.service_date,
            datetime.strptime(meta.service_time or self.options.service_time, "%H:%M").time(),
            tzinfo=self._tz,
        )
        body: dict[str, Any] = {
            "title": self._title(request),
            "status": self.options.status,
            "date": local.strftime("%Y-%m-%dT%H:%M:%S"),
            "date_gmt": local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "terms": self._terms(meta),
        }
        if self.wants_transcript and request.transcript_markdown is not None:
            body["content"] = transcript_html(
                request.transcript_markdown, self.options.content_prefix
            )
            if self.options.full_text and request.transcript_markdown.strip():
                body["full_text"] = True
        if audio_id is not None:
            body["audio_attachment_id"] = audio_id
        return body

    def _title(self, request: PublicationRequest) -> str:
        meta = request.metadata
        for candidate in (meta.title, meta.occasion):
            if candidate and candidate.strip():
                return candidate.strip()
        return f"Sermon, {request.service_date.isoformat()}"

    def _terms(self, meta: SermonMetadata) -> dict[str, list[str]]:
        terms: dict[str, list[str]] = {}
        for field, taxonomy in self.options.term_fields.items():
            raw = getattr(meta, field) if field in METADATA_TERM_FIELDS else meta.extra.get(field)
            values = _values(raw)
            if field == "scripture":
                values = list(
                    dict.fromkeys(
                        normalize_scripture(v, self.options.scripture_dash) for v in values
                    )
                )
            separator = self.options.join_terms.get(field)
            if separator is not None and values:
                values = [separator.join(values)]
            merged = terms.setdefault(taxonomy, [])
            merged += [v for v in values if v not in merged]
        return terms

    def _upload(self, request: PublicationRequest, artifact: ArtifactRef) -> int:
        filename = self.options.filename.format(
            date=request.service_date.isoformat(), slug=slugify(self._title(request))
        )
        data = self._call(
            "POST",
            "/georg/v1/audio",
            params={
                "publication_key": request.publication_key,
                "filename": filename,
                "title": self._title(request),
            },
            content=FileBody(artifact.path),
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Length": str(artifact.path.stat().st_size),
                HEADER_CONTENT_SHA256: file_sha256(artifact.path),
            },
        )
        audio_id = data.get("id")
        if isinstance(audio_id, bool) or not isinstance(audio_id, int):
            raise ModuleError(f"{self.name}: audio upload response has no attachment id")
        return audio_id

    def _artifact(self, request: PublicationRequest) -> ArtifactRef:
        try:
            return request.files[self.options.rendition]
        except KeyError:
            raise ModuleError(
                f"output {self.name!r}: request has no {self.options.rendition!r} rendition"
            ) from None

    def _sermon_route(self, publication_key: str) -> str:
        if not PUBLICATION_KEY_PATTERN.match(publication_key):
            raise ModuleError(f"output {self.name!r}: unsupported publication key")
        return f"/georg/v1/sermons/{publication_key}"

    def _call(
        self, method: str, route: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        data = request_json(
            self._client,
            method,
            self._rest + route,
            provider=f"wordpress {self.name}",
            attempts=self.options.attempts,
            sleep=self._sleep,
            auth=self._auth,
            headers={"Accept": "application/json", **(headers or {})},
            **kwargs,
        )
        if data is not None and not isinstance(data, dict):
            raise ModuleError(f"{self.name}: bridge response is not a JSON object")
        return data

    def _ref(self, data: dict[str, Any]) -> ExternalRef:
        post_id = data.get("id")
        if isinstance(post_id, bool) or not isinstance(post_id, int):
            raise ModuleError(f"{self.name}: bridge response has no post id")
        link = data.get("link")
        return ExternalRef(
            target=self.name,
            external_id=str(post_id),
            url=link if isinstance(link, str) else None,
        )
