# SPDX-License-Identifier: AGPL-3.0-only
"""The web role: login, service list, sermon review, approval, and jobs."""

import json
import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from georg import __version__
from georg.core.config import GeorgConfig
from georg.core.errors import ConfigError
from georg.core.types import ServiceBoundaries
from georg.db.engine import session_factory
from georg.db.models import Artifact, Sermon, SermonRevision
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
from georg.storage.base import ArtifactNotFoundError
from georg.storage.local import LocalStore
from georg.web.auth import (
    SESSION_USER,
    CsrfError,
    CurrentUser,
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
        return CurrentUser(user.id, user.username, user.role)

    DB = Annotated[Session, Depends(db)]
    User = Annotated[CurrentUser, Depends(current_user)]

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
        log_in(request, user.id)
        return _redirect(safe_next(next))

    @app.post("/logout")
    def logout(request: Request) -> Response:
        request.session.clear()
        return _redirect("/login")

    # --- services -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def services(request: Request, user: User, session: DB) -> HTMLResponse:
        sermons = SermonRepository(session)
        rows = []
        for service in ServiceRepository(session).newest_first(limit=200):
            found = sermons.for_service(service.id)
            rows.append({"service": service, "review": _review_state(sermons, found)})
        return render(request, "services.html", {"rows": rows}, user=user)

    @app.get("/services/{service_id}", response_class=HTMLResponse)
    def service_page(request: Request, service_id: int, user: User, session: DB) -> Response:
        service = ServiceRepository(session).get(service_id)
        found = SermonRepository(session).for_service(service_id)
        if found and found[0].current_revision_id is not None:
            return _redirect(f"/sermons/{found[0].id}")
        return render(request, "service.html", {"service": service}, user=user)

    # --- sermon review ----------------------------------------------------------------

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
        published = {p.target_name for p in publications}
        preview = _preview(artifacts)
        seek = preview or next((a for a in audio if a.name == SEEK_RENDITION), None)
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
            "seek": seek,
            "rendered": rendered,
            "rendering": bool(renders),
            "failed_render": failed_render,
            "report": report,
            "revisions": list(reversed(revisions)),
            "approved_ids": {a.revision_id for a in approvals},
            "approval": sermons.current_approval(sermon_id),
            "review_state": _review_state(sermons, [sermon]),
            "publications": publications,
            "unpublished_outputs": [o.name for o in config.outputs if o.name not in published],
            "revision_numbers": numbers,
            "author_name": lambda author: _author_name(users, author),
            "publishing": bool(
                [j for j in active_jobs(PUBLISH) if j.payload.get("sermon_id") == sermon_id]
            ),
            "errors": [],
            "polled": False,
        }

    @app.get("/sermons/{sermon_id}", response_class=HTMLResponse)
    def review(request: Request, sermon_id: int, user: User, session: DB) -> HTMLResponse:
        return render(request, "review.html", review_context(session, sermon_id), user=user)

    @app.get("/sermons/{sermon_id}/render-status", response_class=HTMLResponse)
    def render_status(request: Request, sermon_id: int, user: User, session: DB) -> HTMLResponse:
        context = {**review_context(session, sermon_id), "polled": True}
        return render(request, "_render_status.html", context)

    @app.post("/sermons/{sermon_id}/revisions", response_class=HTMLResponse)
    async def save(request: Request, sermon_id: int, user: User) -> Response:
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
    def restore(request: Request, sermon_id: int, revision_id: int, user: User) -> Response:
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
        user: User,
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
        except NotCurrentRevisionError:
            _flash(request, "A newer revision exists; review it before approving.", "error")
            return _redirect(f"/sermons/{sermon_id}")
        queue.enqueue(PUBLISH, {"sermon_id": sermon_id})
        _flash(request, f"Approved revision {number}; publishing has been queued.")
        return _redirect(f"/sermons/{sermon_id}")

    @app.post("/sermons/{sermon_id}/reprocess")
    def reprocess(request: Request, sermon_id: int, user: User, session: DB) -> Response:
        if user.role != "admin":
            return PlainTextResponse("Reprocessing needs an admin.", status_code=403)
        sermon = SermonRepository(session).get(sermon_id)
        queue.enqueue(PROCESS, {"service_id": sermon.service_id, "reprocess": True})
        _flash(
            request,
            "Reprocessing queued; the fresh proposal will become the current revision.",
        )
        return _redirect(f"/sermons/{sermon_id}")

    @app.post("/sermons/{sermon_id}/publish")
    def publish(request: Request, sermon_id: int, user: User, session: DB) -> Response:
        if not SermonRepository(session).is_approved(sermon_id):
            _flash(request, "Approve the current revision before publishing.", "error")
        else:
            queue.enqueue(PUBLISH, {"sermon_id": sermon_id})
            _flash(request, "Publishing has been queued.")
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
        return render(request, "_jobs_table.html", jobs_context())

    @app.post("/jobs/discover")
    def check_recordings(request: Request, user: User) -> Response:
        if active_jobs(DISCOVER):
            _flash(request, "A check for new recordings is already queued.")
        else:
            queue.enqueue(DISCOVER, {})
            _flash(request, "Checking for new recordings; new services appear when found.")
        return _redirect("/jobs")

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


def _review_state(sermons: SermonRepository, found: Any) -> str:
    sermon: Sermon | None = found[0] if found else None
    if sermon is None or sermon.current_revision_id is None:
        return "no sermon yet"
    approval = sermons.current_approval(sermon.id)
    if approval is not None:
        return f"approved (revision {sermons.revision(approval.revision_id).number})"
    if sermons.approvals(sermon.id):
        return "edited since approval"
    return "needs review"


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


def _preview(artifacts: Any) -> AudioFile | None:
    """The newest whole-recording preview; it starts at recording time 0."""
    found = [
        a for a in artifacts if a.kind == "rendition" and a.meta.get("name") == PREVIEW_RENDITION
    ]
    return AudioFile(found[-1], PREVIEW_RENDITION, 0, True) if found else None


__all__ = ["create_app"]
