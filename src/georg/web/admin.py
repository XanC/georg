# SPDX-License-Identifier: AGPL-3.0-only
"""Admin area (users, read-only settings, connection checks, health) and /account.

Routes are registered on the app by `register`; every admin page needs the admin role,
and every change is written to the audit log.
"""

import os
import re
import shutil
import smtplib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from georg import __version__
from georg.core.checks import CheckResult, ConnectionCheck
from georg.core.config import GeorgConfig
from georg.db.models import USER_ROLES
from georg.db.repositories import AuditRepository, UserRepository
from georg.db.types import utcnow
from georg.jobs.queue import JobQueue
from georg.notify import check_smtp, redact, send_test_email
from georg.pipeline.registry import INPUT_TYPES, OUTPUT_TYPES, build_input, build_output
from georg.web.auth import (
    MIN_PASSWORD_LENGTH,
    CurrentUser,
    Forbidden,
    generate_password,
    hash_password,
    verify_password,
)
from georg.web.settings_view import secret_values, settings_sections

CHECK_TIMEOUT_S: Final = 15.0
FAILED_WINDOW: Final = timedelta(days=7)
AUDIT_LIMIT: Final = 50
NO_TEST: Final = "No test available."
COMMIT_ENV: Final = "GEORG_GIT_COMMIT"
CHECKOUT: Final = Path(__file__).resolve().parents[3]
USERNAME: Final = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
EMAIL: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AdminDeps:
    config: GeorgConfig
    sessions: sessionmaker[Session]
    readers: sessionmaker[Session]
    queue: JobQueue
    render: Callable[..., HTMLResponse]
    redirect: Callable[[str], Response]
    flash: Callable[..., None]


class FormProblem(Exception):
    """A submitted admin form is invalid; the message is shown to the admin."""


def _email(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if not EMAIL.match(value):
        raise FormProblem(f"{value!r} doesn't look like an email address.")
    return value


def _new_password(typed: str) -> tuple[str, bool]:
    """The typed password, or a generated one (True) when the field was left empty."""
    if not typed:
        return generate_password(), True
    if len(typed) < MIN_PASSWORD_LENGTH:
        raise FormProblem(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")
    return typed, False


def _guard(
    users: UserRepository, actor: CurrentUser, target_id: int, *, removes_admin: bool
) -> None:
    """Refuse to lock everyone out: no self-disable/demote, and keep one active admin."""
    if not removes_admin:
        return
    target = users.get(target_id)
    if target.id == actor.id:
        raise FormProblem("You can't disable or demote yourself.")
    if target.role == "admin" and target.disabled_at is None and users.active_admin_count() <= 1:
        raise FormProblem(f"{target.username} is the last active admin.")


def disk(label: str, path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"label": label, "path": str(path), "error": "not available"}
    return {
        "label": label,
        "path": str(path),
        "free": usage.free,
        "total": usage.total,
        "free_pct": 100 * usage.free / usage.total if usage.total else 0.0,
    }


def git_commit(root: Path = CHECKOUT) -> str | None:
    """`$GEORG_GIT_COMMIT`, else the checkout's HEAD when running from a git checkout."""
    if os.environ.get(COMMIT_ENV):
        return os.environ[COMMIT_ENV][:12]
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head.removeprefix("ref: ")
        if (git / ref).is_file():
            return (git / ref).read_text().strip()[:12]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
    except OSError:
        return None
    return None


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def has_check(kind: str, type_name: str) -> bool:
    """Whether the registered module class for `type_name` defines `check()`."""
    target = (INPUT_TYPES if kind == "input" else OUTPUT_TYPES).get(type_name)
    if target is None:
        return False
    module_name, _, attr = target.partition(":")
    try:
        cls = getattr(import_module(module_name), attr)
    except (ImportError, AttributeError):
        return False
    return callable(getattr(cls, "check", None))


def module_check(build: Callable[[], object]) -> CheckResult:
    try:
        module = build()
    except Exception as exc:
        return CheckResult(False, f"Couldn't set up the module: {exc}")
    if not isinstance(module, ConnectionCheck):
        return CheckResult(False, NO_TEST)
    return module.check()


def register(app: FastAPI, deps: AdminDeps, current_user: Callable[..., CurrentUser]) -> None:
    config, sessions, readers, queue = deps.config, deps.sessions, deps.readers, deps.queue
    render, redirect, flash = deps.render, deps.redirect, deps.flash
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="georg-check")
    hidden = secret_values(config)

    def admin_user(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        if not user.is_admin:
            raise Forbidden("Only admins can open the admin area.")
        return user

    Admin = Annotated[CurrentUser, Depends(admin_user)]
    User = Annotated[CurrentUser, Depends(current_user)]

    def audit(
        session: Session, actor: CurrentUser, action: str, target: str | None, **detail: Any
    ) -> None:
        AuditRepository(session).add(actor.id, action, target, detail)

    def run_check(fn: Callable[[], CheckResult]) -> CheckResult:
        def guarded() -> CheckResult:
            try:
                return fn()
            except Exception as exc:
                return CheckResult(False, f"Check failed: {exc}")

        future = executor.submit(guarded)
        try:
            result = future.result(timeout=CHECK_TIMEOUT_S)
        except TimeoutError:
            result = CheckResult(False, f"No answer within {CHECK_TIMEOUT_S:.0f} s.")
        return CheckResult(result.ok, redact(result.message, hidden))

    # --- overview and health ----------------------------------------------------------

    def health() -> dict[str, Any]:
        return {
            "disks": [disk("Data directory", config.data_dir), disk("Root filesystem", Path("/"))],
            "counts": queue.counts(),
            "failed": queue.failed_since(utcnow() - FAILED_WINDOW),
            "version": __version__,
            "commit": git_commit(),
            "last_finished": queue.last_finished_at(),
        }

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request, user: Admin) -> HTMLResponse:
        with readers() as session:
            names = {u.id: u.username for u in UserRepository(session).all()}
            entries = [
                {
                    "at": e.at,
                    "user": names.get(e.user_id, "?") if e.user_id is not None else "system",
                    "action": e.action,
                    "target": e.target,
                    "detail": e.detail,
                }
                for e in AuditRepository(session).recent(AUDIT_LIMIT)
            ]
        context = {"health": health(), "audit": entries, "human_bytes": human_bytes}
        return render(request, "admin.html", context, user=user)

    # --- users ------------------------------------------------------------------------

    def users_page(
        request: Request,
        user: CurrentUser,
        *,
        shown: dict[str, str] | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        with readers() as session:
            users = list(UserRepository(session).all())
        context = {"users": users, "roles": USER_ROLES, "shown": shown, "error": error}
        response = render(
            request, "admin_users.html", context, user=user, status_code=400 if error else 200
        )
        if shown:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, user: Admin) -> HTMLResponse:
        return users_page(request, user)

    @app.post("/admin/users", response_class=HTMLResponse)
    def add_user(
        request: Request,
        user: Admin,
        username: Annotated[str, Form()],
        role: Annotated[str, Form()],
        email: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
    ) -> Response:
        username = username.strip()
        try:
            if not USERNAME.match(username):
                raise FormProblem("Usernames are 1 to 64 letters, digits, '.', '_', '@' or '-'.")
            if role not in USER_ROLES:
                raise FormProblem(f"Unknown role {role!r}.")
            address = _email(email)
            secret, generated = _new_password(password)
            with sessions.begin() as session:
                UserRepository(session).add(username, hash_password(secret), role, email=address)
                audit(session, user, "user.add", username, role=role, generated=generated)
        except FormProblem as exc:
            return users_page(request, user, error=str(exc))
        except IntegrityError:
            return users_page(request, user, error=f"User {username!r} already exists.")
        if generated:
            return users_page(request, user, shown={"username": username, "password": secret})
        flash(request, f"Added {role} {username}.")
        return redirect("/admin/users")

    def change_user(
        request: Request,
        user: CurrentUser,
        user_id: int,
        change: Callable[[Session, UserRepository], str],
    ) -> Response:
        try:
            with sessions.begin() as session:
                message = change(session, UserRepository(session))
        except FormProblem as exc:
            return users_page(request, user, error=str(exc))
        flash(request, message)
        return redirect("/admin/users")

    @app.post("/admin/users/{user_id}/role", response_class=HTMLResponse)
    def set_role(
        request: Request, user_id: int, user: Admin, role: Annotated[str, Form()]
    ) -> Response:
        def change(session: Session, users: UserRepository) -> str:
            if role not in USER_ROLES:
                raise FormProblem(f"Unknown role {role!r}.")
            target = users.get(user_id)
            if target.role == role:
                return f"{target.username} is already {role}."
            _guard(users, user, user_id, removes_admin=target.role == "admin")
            old = target.role
            users.set_role(user_id, role)
            audit(session, user, "user.role", target.username, old=old, new=role)
            return f"{target.username} is now {role}."

        return change_user(request, user, user_id, change)

    @app.post("/admin/users/{user_id}/disable", response_class=HTMLResponse)
    def disable_user(request: Request, user_id: int, user: Admin) -> Response:
        def change(session: Session, users: UserRepository) -> str:
            _guard(users, user, user_id, removes_admin=True)
            target = users.set_disabled(user_id, utcnow())
            audit(session, user, "user.disable", target.username)
            return f"Disabled {target.username}; they are logged out on their next request."

        return change_user(request, user, user_id, change)

    @app.post("/admin/users/{user_id}/enable", response_class=HTMLResponse)
    def enable_user(request: Request, user_id: int, user: Admin) -> Response:
        def change(session: Session, users: UserRepository) -> str:
            target = users.set_disabled(user_id, None)
            audit(session, user, "user.enable", target.username)
            return f"Enabled {target.username}."

        return change_user(request, user, user_id, change)

    @app.post("/admin/users/{user_id}/email", response_class=HTMLResponse)
    def set_user_email(
        request: Request, user_id: int, user: Admin, email: Annotated[str, Form()] = ""
    ) -> Response:
        def change(session: Session, users: UserRepository) -> str:
            target = users.set_email(user_id, _email(email))
            audit(session, user, "user.email", target.username, set=target.email is not None)
            return f"Saved the email address of {target.username}."

        return change_user(request, user, user_id, change)

    @app.post("/admin/users/{user_id}/password", response_class=HTMLResponse)
    def reset_password(
        request: Request, user_id: int, user: Admin, password: Annotated[str, Form()] = ""
    ) -> Response:
        try:
            secret, generated = _new_password(password)
            with sessions.begin() as session:
                target = UserRepository(session).set_password(user_id, hash_password(secret))
                name = target.username
                audit(session, user, "user.password_reset", name, generated=generated)
        except FormProblem as exc:
            return users_page(request, user, error=str(exc))
        if generated:
            return users_page(request, user, shown={"username": name, "password": secret})
        flash(request, f"Changed the password of {name}.")
        return redirect("/admin/users")

    # --- own account ------------------------------------------------------------------

    def account_page(request: Request, user: CurrentUser, error: str | None = None) -> Response:
        with readers() as session:
            email = UserRepository(session).get(user.id).email
        context = {"email": email, "error": error}
        return render(
            request, "account.html", context, user=user, status_code=400 if error else 200
        )

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request, user: User) -> Response:
        return account_page(request, user)

    @app.post("/account/password", response_class=HTMLResponse)
    def change_own_password(
        request: Request,
        user: User,
        current: Annotated[str, Form()],
        new: Annotated[str, Form()],
        confirm: Annotated[str, Form()],
    ) -> Response:
        try:
            if new != confirm:
                raise FormProblem("The new passwords don't match.")
            if len(new) < MIN_PASSWORD_LENGTH:
                raise FormProblem(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")
            with sessions.begin() as session:
                users = UserRepository(session)
                if not verify_password(users.get(user.id).password_hash, current):
                    raise FormProblem("The current password is wrong.")
                users.set_password(user.id, hash_password(new))
                audit(session, user, "account.password", user.username)
        except FormProblem as exc:
            return account_page(request, user, str(exc))
        flash(request, "Your password was changed.")
        return redirect("/account")

    @app.post("/account/email", response_class=HTMLResponse)
    def change_own_email(
        request: Request, user: User, email: Annotated[str, Form()] = ""
    ) -> Response:
        try:
            address = _email(email)
            with sessions.begin() as session:
                UserRepository(session).set_email(user.id, address)
                audit(session, user, "account.email", user.username, set=address is not None)
        except FormProblem as exc:
            return account_page(request, user, str(exc))
        flash(request, "Your email address was saved.")
        return redirect("/account")

    # --- settings and connection checks -----------------------------------------------

    @app.get("/admin/settings", response_class=HTMLResponse)
    def admin_settings(request: Request, user: Admin) -> HTMLResponse:
        with readers() as session:
            email = UserRepository(session).get(user.id).email
        context = {"sections": settings_sections(config, has_check), "my_email": email}
        return render(request, "admin_settings.html", context, user=user)

    def check_response(
        request: Request, user: CurrentUser, target: str, result: CheckResult
    ) -> HTMLResponse:
        with sessions.begin() as session:
            audit(session, user, "check.run", target, ok=result.ok)
        return render(request, "_check_result.html", {"result": result}, user=user)

    @app.post("/admin/checks/{kind}/{name}", response_class=HTMLResponse)
    def run_module_check(request: Request, kind: str, name: str, user: Admin) -> Response:
        build: Callable[[], object]
        if kind == "input":
            found_input = next((i for i in config.inputs if i.name == name), None)
            if found_input is None:
                return PlainTextResponse(f"no input named {name!r}", status_code=404)
            build = lambda: build_input(found_input)  # noqa: E731
        elif kind == "output":
            found_output = next((o for o in config.outputs if o.name == name), None)
            if found_output is None:
                return PlainTextResponse(f"no output named {name!r}", status_code=404)
            build = lambda: build_output(found_output, timezone=config.timezone)  # noqa: E731
        elif kind == "email" and name == "smtp":
            smtp = config.review.smtp
            if smtp is None:
                return check_response(request, user, "email", CheckResult(False, NO_TEST))
            result = run_check(lambda: check_smtp(smtp, smtplib.SMTP))
            return check_response(request, user, "email:smtp", result)
        elif kind == "email" and name == "me":
            return send_me(request, user)
        else:
            return PlainTextResponse(f"unknown check {kind!r}", status_code=404)
        return check_response(
            request, user, f"{kind}:{name}", run_check(lambda: module_check(build))
        )

    def send_me(request: Request, user: CurrentUser) -> HTMLResponse:
        smtp = config.review.smtp
        with readers() as session:
            email = UserRepository(session).get(user.id).email
        if smtp is None:
            result = CheckResult(False, "Email (SMTP) is not configured.")
        elif email is None:
            result = CheckResult(False, "Add your email address on the Account page first.")
        else:
            result = run_check(
                lambda: send_test_email(smtp, email, instance=config.instance, factory=smtplib.SMTP)
            )
        return check_response(request, user, "email:test", result)
