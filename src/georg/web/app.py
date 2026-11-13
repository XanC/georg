# SPDX-License-Identifier: AGPL-3.0-only
"""The web role: login, service list, sermon review, approval, and jobs."""

import json
import os
import time
from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Final
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from georg import __version__
from georg.core.config import GeorgConfig, OutputConfig
from georg.core.errors import ConfigError
from georg.core.types import ServiceBoundaries
from georg.db.engine import session_factory
from georg.db.models import Artifact, Publication, SermonRevision, Service
from georg.db.repositories import (
    ArtifactRepository,
    NotCurrentRevisionError,
    NotFoundError,
    PublicationRepository,
    SermonRepository,
    ServiceRepository,
    UserRepository,
)
from georg.db.types import utcnow
from georg.idle import EXEMPT_PATHS, ActivityMarker, idle_state
from georg.jobs.queue import JobInfo, JobQueue
from georg.pipeline.registry import build_output
from georg.pipeline.service import pending_request
from georg.storage.base import ArtifactNotFoundError
from georg.storage.local import LocalStore
from georg.web import admin
from georg.web.auth import (
    SESSION_USER,
    CsrfError,
    CurrentUser,
    Forbidden,
    LoginRequired,
    csrf_token,
    log_in,
    safe_next,
    verify_csrf,
    verify_password,
)
from georg.web.forms import (
    BOUNDARY_FIELDS,
    FormError,
    ReviewEdit,
    form_values,
    format_hms,
    format_tenths,
    parse_edit,
)

SECRET_ENV: Final = "GEORG_SECRET_KEY"
SOURCE_URL_ENV: Final = "GEORG_SOURCE_URL"
DEFAULT_SOURCE_URL: Final = "https://github.com/XanC/georg"
RENDER: Final = "render"
PUBLISH: Final = "publish"
DISCOVER: Final = "discover"
PROCESS: Final = "process"
ACTIVE_STATUSES: Final = ("queued", "running")
SEEK_RENDITION: Final = "service_hq"
PREVIEW_RENDITION: Final = "preview"
WEBSITE_CACHE_S: Final = 60.0
RECENT_DAYS: Final = 30
"""The Services list shows services fetched this recently, plus any needing attention."""
CONFLICT_STATUS: Final = "Website edited: needs decision"
ATTENTION_STATUSES: Final = (
    "Needs review",
    "Edited since approval",
    "Publish failed",
    CONFLICT_STATUS,
    "Processing failed",
)
CUT_PREVIEW_LABELS: Final = {"sermon_preview": "Sermon", "service_preview": "Service"}
"""Built-in low-quality copies of the cuts (`georg.pipeline.local.CUT_PREVIEWS`)."""
AUDIO_KINDS: Final = ("sermon_audio", "rendition")
SESSION_FLASH: Final = "flash"
_HERE = Path(__file__).parent
_SECURITY_HEADERS: Final = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@dataclass(frozen=True)
class AudioFile:
    artifact: Artifact
    name: str
    origin_ms: int | None
    """Source-timeline position of the file's first sample, when known."""
    current: bool
    """Cut at the current revision's boundaries."""
    label: str = ""


def _bounds_key(b: ServiceBoundaries) -> list[int]:
    return [b.service_start_ms, b.sermon_start_ms, b.sermon_end_ms, b.service_end_ms]


def _flash(request: Request, message: str, level: str = "info") -> None:
    request.session.setdefault(SESSION_FLASH, []).append([level, message])


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def create_app(
    config: GeorgConfig,
    engine: Engine,
    *,
    secret_key: str | None = None,
    https_only: bool = False,
    store: LocalStore | None = None,
    queue: JobQueue | None = None,
    source_url: str | None = None,
    activity: ActivityMarker | None = None,
) -> FastAPI:
    """Build the review UI. The session secret defaults to `$GEORG_SECRET_KEY`."""
    secret_key = secret_key or os.environ.get(SECRET_ENV)
    if not secret_key:
        raise ConfigError(f"environment variable {SECRET_ENV} is not set")
    store = store or LocalStore.for_data_dir(config.data_dir)
    queue = queue or JobQueue(engine)
    sessions = session_factory(engine)
    readers = session_factory(engine, immediate=False)
    source_url = source_url or os.environ.get(SOURCE_URL_ENV, DEFAULT_SOURCE_URL)
    if activity is None and config.idle_shutdown_minutes is not None:
        activity = ActivityMarker.for_data_dir(config.data_dir)
    idle_after = (
        None
        if config.idle_shutdown_minutes is None
        else timedelta(minutes=config.idle_shutdown_minutes)
    )
    clock = activity.clock if activity is not None else utcnow
    started = clock()

    app = FastAPI(
        title="Georg",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(verify_csrf)],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="georg_session",
        same_site="lax",
        https_only=https_only,
    )
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.globals.update(version=__version__, source_url=source_url)
    templates.env.filters["hms"] = format_hms
    templates.env.filters["tenths"] = format_tenths

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if activity is not None and request.url.path not in EXEMPT_PATHS:
            activity.touch()
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired) -> Response:
        target = "/login"
        if request.method == "GET" and request.url.path != "/":
            target += "?next=" + request.url.path
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": target})
        return _redirect(target)

    @app.exception_handler(CsrfError)
    async def csrf_failed(request: Request, _: CsrfError) -> Response:
        return PlainTextResponse(
            "Form expired or invalid (CSRF check failed). Go back, reload, and try again.",
            status_code=403,
        )

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> Response:
        return PlainTextResponse(str(exc) or "Not allowed.", status_code=403)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> Response:
        return PlainTextResponse(str(exc), status_code=404)

    def db() -> Iterator[Session]:
        with readers() as session:
            yield session

    def current_user(request: Request, session: Annotated[Session, Depends(db)]) -> CurrentUser:
        user_id = request.session.get(SESSION_USER)
        if not isinstance(user_id, int):
            raise LoginRequired
        try:
            user = UserRepository(session).get(user_id)
        except NotFoundError:
            request.session.clear()
            raise LoginRequired from None
        if user.disabled_at is not None:
            request.session.clear()
            raise LoginRequired
        return CurrentUser(user.id, user.username, user.role, user.email)

    def editor_user(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        if not user.can_edit:
            raise Forbidden("Viewers can look but not change anything.")
        return user

    DB = Annotated[Session, Depends(db)]
    User = Annotated[CurrentUser, Depends(current_user)]
    Editor = Annotated[CurrentUser, Depends(editor_user)]

    def render(
        request: Request,
        name: str,
        context: dict[str, Any],
        *,
        user: CurrentUser | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        fragment = name.startswith("_")
        flashes = [] if fragment else request.session.pop(SESSION_FLASH, [])
        return templates.TemplateResponse(
            request,
            name,
            {"user": user, "csrf_token": csrf_token(request), "flashes": flashes, **context},
            status_code=status_code,
        )

    def active_jobs(kind: str) -> list[JobInfo]:
        return [
            job
            for status in ACTIVE_STATUSES
            for job in queue.list(status=status)
            if job.kind == kind
        ]

    # --- health and auth -------------------------------------------------------------

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.post("/keepalive", status_code=204)
    def keepalive(user: User) -> Response:
        if activity is not None:
            activity.touch(force=True)
        return Response(status_code=204)

    @app.get("/idle-status")
    def idle_status(user: User, session: DB) -> dict[str, Any]:
        if idle_after is None or activity is None:
            return {"enabled": False, "busy": False, "seconds_left": None}
        state = idle_state(session.connection(), activity, started)
        left = state.remaining(idle_after, clock())
        return {"enabled": True, "busy": state.busy, "seconds_left": int(left.total_seconds())}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str | None = None) -> HTMLResponse:
        return render(request, "login.html", {"next": safe_next(next), "error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        session: DB,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/",
    ) -> Response:
        user = UserRepository(session).by_username(username.strip())
        if not verify_password(user.password_hash if user else None, password) or user is None:
            context = {"next": safe_next(next), "error": "Wrong username or password."}
            return render(request, "login.html", context, status_code=401)
        if user.disabled_at is not None:
            context = {"next": safe_next(next), "error": "This account is disabled."}
            return render(request, "login.html", context, status_code=403)
        user_id = user.id
        with sessions.begin() as writer:
            UserRepository(writer).record_login(user_id, utcnow())
        log_in(request, user_id)
        return _redirect(safe_next(next))

    @app.post("/logout")
    def logout(request: Request) -> Response:
        request.session.clear()
        return _redirect("/login")

    # --- services -----------------------------------------------------------------

    website_cache: dict[tuple[str, date, date], tuple[float, list[dict[str, Any]] | None]] = {}

    def website_sermons(target: OutputConfig, first: date, last: date) -> list[dict[str, Any]]:
        """The target's published sermons dated first..last, cached (failures too) for
        WEBSITE_CACHE_S so reloading a page doesn't ask the website again."""
        key = (target.name, first, last)
        now = time.monotonic()
        hit = website_cache.get(key)
        if hit is None or now - hit[0] >= WEBSITE_CACHE_S:
            for stale in [k for k, (t, _) in website_cache.items() if now - t >= WEBSITE_CACHE_S]:
                del website_cache[stale]
            posts: list[dict[str, Any]] | None
            try:
                lookup = getattr(
                    build_output(target, timezone=config.timezone), "sermons_between", None
                )
                posts = list(lookup(first, last)) if callable(lookup) else []
            except Exception:
                posts = None
            hit = website_cache[key] = (now, posts)
        if hit[1] is None:
            raise LookupError(f"{target.name}: sermon lookup failed")
        return hit[1]

    def same_date_posts(session: Session, services: Sequence[Service]) -> dict[int, dict[str, Any]]:
        """Per service id: the posts the website already has on the sermon's date that
        aren't its own publication, and the targets whose lookup failed (never raised).
        One lookup per wordpress target covers every service's date."""
        targets = [o for o in config.outputs if o.type == "wordpress"]
        if not targets:
            return {}
        sermons = SermonRepository(session)
        pubs = PublicationRepository(session)
        days: dict[int, date] = {}
        own: dict[int, set[tuple[str, str | None]]] = {}
        for service in services:
            found = sermons.for_service(service.id)
            revision = sermons.current_revision(found[0].id) if found else None
            meta_day = sermons.metadata(revision).service_date if revision else None
            day = meta_day or service.service_date
            if day is None:
                continue
            days[service.id] = day
            own[service.id] = {
                (p.target_name, p.external_id) for s in found for p in pubs.for_sermon(s.id)
            }
        if not days:
            return {}
        first, last = min(days.values()), max(days.values())
        result: dict[int, dict[str, Any]] = {
            sid: {"date": day, "posts": [], "failed": []} for sid, day in days.items()
        }
        for target in targets:
            try:
                posts = website_sermons(target, first, last)
            except LookupError:
                for row in result.values():
                    row["failed"].append(target.name)
                continue
            for sid, day in days.items():
                result[sid]["posts"] += [
                    p
                    for p in posts
                    if p.get("date") == day.isoformat() and (target.name, p["id"]) not in own[sid]
                ]
        return result

    def statuses(session: Session, services: Sequence[Service]) -> dict[int, str]:
        sermons = SermonRepository(session)
        pubs = PublicationRepository(session)
        publishing = {j.payload.get("sermon_id") for j in active_jobs(PUBLISH)}
        configured = [o.name for o in config.outputs]
        failed = _failed_processing(queue.list())
        return {
            s.id: _service_status(s, sermons, pubs, configured, publishing, failed)
            for s in services
        }

    @app.get("/", response_class=HTMLResponse)
    def services(
        request: Request,
        user: User,
        session: DB,
        show_all: Annotated[bool, Query(alias="all")] = False,
    ) -> HTMLResponse:
        found = ServiceRepository(session).newest_first(limit=200)
        status = statuses(session, found)
        same_date = same_date_posts(session, found)
        recent = utcnow() - timedelta(days=RECENT_DAYS)
        rows = []
        for service in found:
            posts = same_date.get(service.id, {}).get("posts", [])
            row = {"service": service, "status": status[service.id], "same_date": posts}
            row["shown"] = service.discovered_at >= recent or _needs_attention(row["status"], posts)
            rows.append(row)
        visible = rows if show_all else [r for r in rows if r["shown"]]
        context = {
            "rows": visible,
            "show_all": show_all,
            "hidden": len(rows) - len([r for r in rows if r["shown"]]),
            "recent_days": RECENT_DAYS,
        }
        return render(request, "services.html", context, user=user)

    @app.get("/services/{service_id}", response_class=HTMLResponse)
    def service_page(request: Request, service_id: int, user: User, session: DB) -> Response:
        service = ServiceRepository(session).get(service_id)
        found = SermonRepository(session).for_service(service_id)
        if found and found[0].current_revision_id is not None:
            return _redirect(f"/sermons/{found[0].id}")
        context = {
            "service": service,
            "status": statuses(session, [service])[service.id],
            "same_date": same_date_posts(session, [service]).get(service.id),
            "retention_days": config.retention.days,
        }
        return render(request, "service.html", context, user=user)

    # --- sermon review ----------------------------------------------------------------

    def publish_payload(session: Session, sermon_id: int) -> dict[str, Any] | None:
        """The publish job for an approval: every output. A website that already holds the
        sermon gets only the fields changed since it last accepted a revision, and refuses
        fields edited there (a conflict, resolved through "Update website…")."""
        return {"sermon_id": sermon_id}

    def website_updates(session: Session, sermon_id: int) -> list[dict[str, Any]]:
        """Per website already holding the sermon: the fields an update would send, and
        those edited on the website since Georg wrote them, from the bridge's status
        route (falling back to a recorded conflict if the website can't be reached)."""
        pubs = PublicationRepository(session).for_sermon(sermon_id)
        held = _held_websites(config, pubs)
        updates = []
        for pub in pubs:
            if pub.target_name not in held:
                continue
            target = next(o for o in config.outputs if o.name == pub.target_name)
            edited = set(pub.conflict_fields or [])
            by, at = pub.conflict_by, pub.conflict_at
            failed = False
            try:
                site: Any = build_output(
                    target.model_copy(update={"options": {**target.options, "attempts": 1}}),
                    timezone=config.timezone,
                )
                request = pending_request(config, session, sermon_id, site, store=store)
                sending = site.fields_to_send(request) if request else frozenset()
                status = site.site_status(pub.publication_key, pub.published_at)
                labels = {f: site.field_label(f) for f in {*(sending or ()), *edited}}
                if status is not None:
                    edited = set(status["edited"])
                    by, at = status["modified_by"], status["modified_at"]
                    labels |= {f: site.field_label(f) for f in edited}
            except Exception:
                failed, sending, labels = True, set(pub.conflict_fields or []), {}
            fields = sorted(sending) if sending is not None else ["everything"]
            conflict = sorted(edited & set(fields)) or (
                sorted(edited) if pub.status == "conflict" else []
            )
            when = (
                [at.astimezone(ZoneInfo(config.timezone)).strftime("%b %-d, %Y %-I:%M %p")]
                if at
                else []
            )
            updates.append(
                {
                    "name": pub.target_name,
                    "fields": [(f, labels.get(f, f)) for f in fields],
                    "conflict": [(f, labels.get(f, f)) for f in conflict],
                    "note": "Content was edited on the website ("
                    + ", ".join([*when, *([f"by {by}"] if by else [])])
                    + ")",
                    "failed": failed,
                }
            )
        return updates

    def review_context(session: Session, sermon_id: int) -> dict[str, Any]:
        sermons = SermonRepository(session)
        sermon = sermons.get(sermon_id)
        revision = sermons.current_revision(sermon_id)
        if revision is None:
            raise NotFoundError(f"sermon {sermon_id} has no revision yet")
        service = ServiceRepository(session).get(sermon.service_id)
        boundaries = sermons.boundaries(revision)
        artifacts = ArtifactRepository(session).for_service(service.id)
        report = _load_report(store, artifacts)
        audio = _audio_files(artifacts, boundaries, report)
        rendered = any(a.current for a in audio) or (
            revision.sermon_audio_artifact_id is not None
            and not any(a.artifact.meta.get("boundaries") for a in audio)
        )
        revisions = sermons.revisions(sermon_id)
        revision_ids = {r.id for r in revisions}
        renders = [j for j in active_jobs(RENDER) if j.payload.get("revision_id") in revision_ids]
        failed_render = next(
            (
                j
                for j in reversed(queue.list(status="failed"))
                if j.kind == RENDER and j.payload.get("revision_id") == revision.id
            ),
            None,
        )
        approvals = sermons.approvals(sermon_id)
        users = UserRepository(session)
        numbers = {r.id: r.number for r in revisions}
        publications = PublicationRepository(session).for_sermon(sermon_id)
        publishing = any(j.payload.get("sermon_id") == sermon_id for j in active_jobs(PUBLISH))
        preview = _preview(artifacts)
        cut_previews = _cut_previews(artifacts, boundaries)
        seek = preview or next(
            (a for a in [*cut_previews, *audio] if a.name in ("service_preview", SEEK_RENDITION)),
            None,
        )
        if service.purged_at is not None:
            audio, preview, cut_previews, seek = [], None, [], None
        return {
            "service": service,
            "sermon": sermon,
            "revision": revision,
            "values": form_values(
                boundaries, sermons.metadata(revision), revision.transcript_markdown
            ),
            "boundary_fields": BOUNDARY_FIELDS,
            "audio": audio,
            "preview": preview,
            "cut_previews": cut_previews,
            "hidden": set(config.review.hidden_fields),
            "preacher_suggestions": (
                []
                if "preacher" in config.review.hidden_fields
                else sermons.preacher_suggestions(
                    sermons.metadata(revision).service_date or service.service_date,
                    recent_days=config.review.preacher_recent_days,
                    seeds=config.review.preachers,
                    exclude_sermon_id=sermon_id,
                )
            ),
            "seek": seek,
            "rendered": rendered,
            "rendering": bool(renders),
            "failed_render": failed_render,
            "report": report,
            "revisions": list(reversed(revisions)),
            "approved_ids": {a.revision_id for a in approvals},
            "approval": sermons.current_approval(sermon_id),
            "status": statuses(session, [service])[service.id],
            "outputs": _output_states(
                [o.name for o in config.outputs], publications, revision.id, publishing
            ),
            "revision_numbers": numbers,
            "published_once": any(p.published_revision_id for p in publications),
            "author_name": lambda author: _author_name(users, author),
            "publishing": publishing,
            "purged": service.purged_at,
            "can_reprocess": _can_reprocess(service, publications),
            "retention_days": config.retention.days,
            "errors": [],
            "polled": False,
        }

    @app.get("/sermons/{sermon_id}", response_class=HTMLResponse)
    def review(request: Request, sermon_id: int, user: User, session: DB) -> HTMLResponse:
        context = review_context(session, sermon_id)
        context["same_date"] = same_date_posts(session, [context["service"]]).get(
            context["service"].id
        )
        context["website_updates"] = website_updates(session, sermon_id)
        return render(request, "review.html", context, user=user)

    @app.get("/sermons/{sermon_id}/render-status", response_class=HTMLResponse)
    def render_status(request: Request, sermon_id: int, user: User, session: DB) -> HTMLResponse:
        context = {**review_context(session, sermon_id), "polled": True}
        return render(request, "_render_status.html", context, user=user)

    @app.get("/sermons/{sermon_id}/publish-status", response_class=HTMLResponse)
    def publish_status(request: Request, sermon_id: int, user: User, session: DB) -> HTMLResponse:
        context = review_context(session, sermon_id)
        return render(request, "_publish_status.html", context, user=user)

    @app.post("/sermons/{sermon_id}/revisions", response_class=HTMLResponse)
    async def save(request: Request, sermon_id: int, user: Editor) -> Response:
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        return await run_in_threadpool(lambda: _save(request, sermon_id, user, form))

    def _save(
        request: Request, sermon_id: int, user: CurrentUser, form: dict[str, str]
    ) -> Response:
        with sessions.begin() as session:
            sermons = SermonRepository(session)
            current = sermons.current_revision(sermon_id)
            if current is None:
                raise NotFoundError(f"sermon {sermon_id} has no revision yet")
            errors: list[str] = []
            if form.get("base_revision_id") != str(current.id):
                errors = [
                    f"Revision {current.number} was saved while you were editing; "
                    "your changes were not saved. Copy them and reload the page."
                ]
            else:
                try:
                    edit = parse_edit(
                        form,
                        sermons.boundaries(current),
                        sermons.metadata(current),
                        current.transcript_markdown,
                        hidden=config.review.hidden_fields,
                    )
                except FormError as exc:
                    errors = exc.errors
            if not errors and edit.changed:
                revision = _add_user_revision(sermons, sermon_id, current, edit, user)
                number, revision_id = revision.number, revision.id
        if errors:
            with readers() as session:
                context = review_context(session, sermon_id)
            context["values"] = {**context["values"], **form}
            context["errors"] = errors
            return render(request, "review.html", context, user=user, status_code=400)
        if not edit.changed:
            _flash(request, "No changes to save.")
            return _redirect(f"/sermons/{sermon_id}")
        if edit.boundaries_changed:
            queue.enqueue(RENDER, {"revision_id": revision_id})
            _flash(request, f"Saved revision {number}. The audio is being re-rendered.")
        else:
            _flash(request, f"Saved revision {number}.")
        return _redirect(f"/sermons/{sermon_id}")

    @app.post("/sermons/{sermon_id}/revisions/{revision_id}/restore")
    def restore(request: Request, sermon_id: int, revision_id: int, user: Editor) -> Response:
        with sessions.begin() as session:
            sermons = SermonRepository(session)
            old = sermons.revision(revision_id)
            current = sermons.current_revision(sermon_id)
            if old.sermon_id != sermon_id or current is None:
                raise NotFoundError(f"revision {revision_id} not found for sermon {sermon_id}")
            moved = sermons.boundaries(old) != sermons.boundaries(current)
            new = sermons.restore(revision_id, author=f"user:{user.id}")
            message = f"Restored revision {old.number} as revision {new.number}."
            new_id = new.id
        if moved:
            queue.enqueue(RENDER, {"revision_id": new_id})
            message += " The audio is being re-rendered."
        _flash(request, message)
        return _redirect(f"/sermons/{sermon_id}")

    @app.post("/sermons/{sermon_id}/approve")
    def approve(
        request: Request,
        sermon_id: int,
        user: Editor,
        revision_id: Annotated[int, Form()],
        note: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            with sessions.begin() as session:
                sermons = SermonRepository(session)
                if sermons.revision(revision_id).sermon_id != sermon_id:
                    raise NotFoundError(f"revision {revision_id} not found")
                sermons.approve(revision_id, approved_by=user.id, note=note.strip() or None)
                number = sermons.revision(revision_id).number
                payload = publish_payload(session, sermon_id)
        except NotCurrentRevisionError:
            _flash(request, "A newer revision exists; review it before approving.", "error")
            return _redirect(f"/sermons/{sermon_id}")
        if payload:
            queue.enqueue(PUBLISH, payload)
        _flash(request, f"Approved revision {number}.")
        return _redirect(f"/sermons/{sermon_id}")

    @app.post("/sermons/{sermon_id}/reprocess")
    def reprocess(request: Request, sermon_id: int, user: Editor, session: DB) -> Response:
        if user.role != "admin":
            return PlainTextResponse("Reprocessing needs an admin.", status_code=403)
        sermon = SermonRepository(session).get(sermon_id)
        service = ServiceRepository(session).get(sermon.service_id)
        if not _can_reprocess(service, PublicationRepository(session).for_sermon(sermon_id)):
            _flash(request, "This sermon is published; reprocessing would replace it.", "error")
            return _redirect(f"/sermons/{sermon_id}")
        queue.enqueue(PROCESS, {"service_id": sermon.service_id, "reprocess": True})
        _flash(
            request,
            "Reprocessing queued; the fresh proposal will become the current revision.",
        )
        return _redirect(f"/sermons/{sermon_id}")

    @app.post("/sermons/{sermon_id}/publish")
    def publish(
        request: Request,
        sermon_id: int,
        user: Editor,
        session: DB,
        website: Annotated[str, Form()] = "",
        decision: Annotated[str, Form()] = "",
        fields: Annotated[list[str] | None, Form()] = None,
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        """Publish again, or (with `website`) update a website that already holds the
        sermon, skipping or overwriting `fields` edited there if the reviewer decided so."""
        payload: dict[str, Any] | None
        if not SermonRepository(session).is_approved(sermon_id):
            _flash(request, "Approve the current revision before publishing.", "error")
            return _redirect(f"/sermons/{sermon_id}")
        if not website:
            payload = publish_payload(session, sermon_id)
        elif website not in {o.name for o in config.outputs if o.type == "wordpress"}:
            raise NotFoundError(f"no website output {website!r}")
        elif decision == "overwrite" and confirm != "yes":
            _flash(request, "Confirm that the website's edits may be overwritten.", "error")
            return _redirect(f"/sermons/{sermon_id}")
        else:
            payload = {"sermon_id": sermon_id, "targets": [website]}
            if decision in ("skip", "overwrite") and fields:
                payload[decision] = sorted(set(fields))
        if payload:
            queue.enqueue(PUBLISH, payload)
        return _redirect(f"/sermons/{sermon_id}")

    # --- artifacts --------------------------------------------------------------------

    @app.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: int, user: User, session: DB) -> Response:
        row = ArtifactRepository(session).get(artifact_id)
        try:
            path = store.path(row.sha256)
        except ArtifactNotFoundError:
            return PlainTextResponse("artifact file missing from the store", status_code=404)
        name = str(row.meta.get("name") or f"artifact-{row.id}")
        suffix = {"audio/mpeg": ".mp3", "application/json": ".json"}.get(row.mime, "")
        return FileResponse(
            path,
            media_type=row.mime,
            filename=name + suffix,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=86400, immutable"},
        )

    # --- jobs -------------------------------------------------------------------------

    def jobs_context() -> dict[str, Any]:
        jobs = [j for s in (*ACTIVE_STATUSES, "failed") for j in queue.list(status=s)]
        jobs.sort(key=lambda j: j.id, reverse=True)
        return {"jobs": jobs, "discovering": bool(active_jobs(DISCOVER))}

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, user: User) -> HTMLResponse:
        return render(request, "jobs.html", jobs_context(), user=user)

    @app.get("/jobs/table", response_class=HTMLResponse)
    def jobs_table(request: Request, user: User) -> HTMLResponse:
        return render(request, "_jobs_table.html", jobs_context(), user=user)

    @app.post("/jobs/discover")
    def check_recordings(request: Request, user: Editor) -> Response:
        if active_jobs(DISCOVER):
            _flash(request, "A check for new recordings is already queued.")
        else:
            queue.enqueue(DISCOVER, {})
            _flash(request, "Checking for new recordings; new services appear when found.")
        return _redirect("/jobs")

    admin.register(
        app,
        admin.AdminDeps(config, sessions, readers, queue, render, _redirect, _flash),
        current_user,
    )
    return app


def _add_user_revision(
    sermons: SermonRepository,
    sermon_id: int,
    current: SermonRevision,
    edit: ReviewEdit,
    user: CurrentUser,
) -> SermonRevision:
    origins = {**current.field_origins, **dict.fromkeys(edit.changed, "user")}
    return sermons.add_revision(
        sermon_id,
        author=f"user:{user.id}",
        boundaries=edit.boundaries,
        metadata=edit.metadata,
        transcript_markdown=edit.transcript,
        field_origins=origins,
        sermon_audio_artifact_id=None
        if edit.boundaries_changed
        else current.sermon_audio_artifact_id,
    )


def _service_status(
    service: Service,
    sermons: SermonRepository,
    pubs: PublicationRepository,
    configured: list[str],
    publishing: set[Any],
    failed_processing: Collection[int] = (),
) -> str:
    """One live status line for a service, from its sermon's approval and publications."""
    found = sermons.for_service(service.id)
    sermon = found[0] if found else None
    if sermon is None or sermon.current_revision_id is None:
        if service.id in failed_processing:
            return "Processing failed"
        return service.status_summary or "processing"
    approval = sermons.current_approval(sermon.id)
    if approval is None:
        return "Edited since approval" if sermons.approvals(sermon.id) else "Needs review"
    approved = approval.revision_id
    states = [
        s
        for s in _output_states(
            configured, pubs.for_sermon(sermon.id), approved, sermon.id in publishing
        )
        if s.name in configured
    ]
    failed = [s.name for s in states if s.status == "failed"]
    if failed:
        return f"Publish failed ({', '.join(failed)})"
    if any(s.status == "conflict" for s in states):
        return CONFLICT_STATUS
    if states and all(
        s.status == "published" and s.publication and s.publication.revision_id == approved
        for s in states
    ):
        return "Published"
    if any(
        s.status == "publishing"
        or (s.publication and s.publication.status in ("pending", "publishing"))
        for s in states
    ):
        return "Publishing"
    return "Approved"


def _can_reprocess(service: Service, publications: Sequence[Publication]) -> bool:
    """Published sermons are reprocessed only after retention removed their files; the
    new revision then updates the existing publications on approval."""
    return service.purged_at is not None or not publications


def _failed_processing(jobs: Sequence[JobInfo]) -> set[int]:
    """Services whose latest process job failed for good."""
    latest: dict[int, str] = {}
    for job in sorted(jobs, key=lambda j: j.id):
        service_id = job.payload.get("service_id")
        if job.kind == PROCESS and isinstance(service_id, int):
            latest[service_id] = job.status
    return {service_id for service_id, status in latest.items() if status == "failed"}


def _needs_attention(status: str, same_date_posts: Sequence[object]) -> bool:
    """Whether the Services list keeps showing a service after RECENT_DAYS. A sermon
    already on the website from outside Georg doesn't need reviewing here."""
    if status == "Needs review" and same_date_posts:
        return False
    return status.startswith(ATTENTION_STATUSES)


def _author_name(users: UserRepository, author: str) -> str:
    if not author.startswith("user:"):
        return author
    try:
        return users.get(int(author.removeprefix("user:"))).username
    except (NotFoundError, ValueError):
        return author


def _load_report(store: LocalStore, artifacts: Any) -> dict[str, Any] | None:
    """The newest boundary proposal report (report.json), if readable."""
    reports = [a for a in artifacts if a.kind == "boundary_proposal"]
    if not reports:
        return None
    try:
        data = json.loads(store.path(reports[-1].sha256).read_text(encoding="utf-8"))
    except (ArtifactNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _audio_files(
    artifacts: Any, boundaries: ServiceBoundaries, report: dict[str, Any] | None
) -> list[AudioFile]:
    """One file per rendition name: cut at the current boundaries if any, else the newest.

    A service-span rendition starts at its cut's service start, taken from the
    artifact's `boundaries` meta or, for older artifacts, from the report.
    """
    key = _bounds_key(boundaries)
    report_start = None
    if report and isinstance(report.get("boundaries"), dict):
        value = report["boundaries"].get("service_start_ms")
        report_start = value if isinstance(value, int) else None
    chosen: dict[str, AudioFile] = {}
    for a in artifacts:
        if a.kind not in AUDIO_KINDS or a.meta.get("name") == PREVIEW_RENDITION:
            continue
        if a.meta.get("name") in CUT_PREVIEW_LABELS:
            continue
        name = str(a.meta.get("name") or a.kind)
        cut = a.meta.get("boundaries")
        current = cut == key
        origin = cut[0] if isinstance(cut, list) and cut else report_start
        if a.kind == "sermon_audio":
            origin = cut[1] if isinstance(cut, list) and len(cut) > 1 else None
        previous = chosen.get(name)
        if previous is None or current or not previous.current:
            chosen[name] = AudioFile(a, name, origin, current)
    return sorted(chosen.values(), key=lambda f: (f.artifact.kind != "sermon_audio", f.name))


@dataclass(frozen=True)
class OutputState:
    name: str
    status: str
    label: str
    publication: Publication | None


def _held_websites(config: GeorgConfig, publications: Sequence[Publication]) -> set[str]:
    """Website outputs that already hold some revision of the sermon (or wait for a
    decision on one): they get only changed fields, and "Update website…" on the review page."""
    websites = {o.name for o in config.outputs if o.type == "wordpress"}
    return {
        p.target_name
        for p in publications
        if p.target_name in websites and (p.published_revision_id or p.status == "conflict")
    }


def _output_states(
    configured: list[str],
    publications: Sequence[Publication],
    revision_id: int,
    publishing: bool,
) -> list[OutputState]:
    """What each output shows: a queued or running publish job wins over stored state,
    except for outputs that already hold the current revision (the job skips them)."""
    by_name = {p.target_name: p for p in publications}
    names = [*configured, *(p.target_name for p in publications if p.target_name not in configured)]
    states = []
    for name in names:
        pub = by_name.get(name)
        current = pub is not None and pub.status == "published" and pub.revision_id == revision_id
        if publishing and not current:
            states.append(OutputState(name, "publishing", "Publishing…", pub))
        elif pub is None:
            states.append(OutputState(name, "", "Not published", None))
        elif pub.status == "published":
            states.append(OutputState(name, "published", "Published", pub))
        elif pub.status == "failed":
            states.append(OutputState(name, "failed", f"Failed: {pub.last_error}", pub))
        elif pub.status == "conflict":
            states.append(OutputState(name, "conflict", "Website edited: needs decision", pub))
        else:
            states.append(OutputState(name, "", "Not finished", pub))
    return states


def _preview(artifacts: Any) -> AudioFile | None:
    """The newest whole-recording preview; it starts at recording time 0."""
    found = [
        a for a in artifacts if a.kind == "rendition" and a.meta.get("name") == PREVIEW_RENDITION
    ]
    return AudioFile(found[-1], PREVIEW_RENDITION, 0, True) if found else None


def _cut_previews(artifacts: Any, boundaries: ServiceBoundaries) -> list[AudioFile]:
    """The low-quality copy of each cut: at the current boundaries if any, else the newest."""
    key = _bounds_key(boundaries)
    chosen: dict[str, AudioFile] = {}
    for a in artifacts:
        name = a.meta.get("name")
        cut = a.meta.get("boundaries")
        if a.kind != "rendition" or name not in CUT_PREVIEW_LABELS or not isinstance(cut, list):
            continue
        start, end = (cut[1], cut[2]) if name == "sermon_preview" else (cut[0], cut[3])
        times = f"{format_tenths(start)}\N{EN DASH}{format_tenths(end)}"
        label = f"{CUT_PREVIEW_LABELS[name]} (as cut, {times})"
        previous = chosen.get(name)
        if previous is None or cut == key or not previous.current:
            chosen[name] = AudioFile(a, name, start, cut == key, label)
    return sorted(chosen.values(), key=lambda f: f.name != "sermon_preview")


__all__ = ["create_app"]
