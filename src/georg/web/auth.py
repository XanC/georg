# SPDX-License-Identifier: AGPL-3.0-only
"""Password hashing, login sessions, and CSRF tokens."""

import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))
SESSION_USER = "user_id"
SESSION_CSRF = "csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str


class LoginRequired(Exception):
    """Raised by the `current_user` dependency when nobody is logged in."""


class CsrfError(Exception):
    """A state-changing request arrived without the session's CSRF token."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Check a password; with no hash, spend the same time and return False."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


async def verify_csrf(request: Request) -> None:
    """App-wide dependency: reject unsafe requests whose token doesn't match."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(SESSION_CSRF)
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            value = (await request.form()).get(CSRF_FIELD)
            sent = value if isinstance(value, str) else None
    if not isinstance(expected, str) or sent is None or not secrets.compare_digest(expected, sent):
        raise CsrfError


def log_in(request: Request, user_id: int) -> None:
    request.session.clear()
    request.session[SESSION_USER] = user_id
    csrf_token(request)


def safe_next(target: str | None) -> str:
    """A local path to return to after login, never another site."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
