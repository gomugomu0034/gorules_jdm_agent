"""Session identity for the studio.

Every request acts as exactly one *owner*, a string of the form ``user:<id>``
for a signed-in account or ``guest:<sid>`` for an anonymous visitor. Guests are
first-class: they get a session on their first request without signing up, and
their graphs are stored and scoped exactly like a user's.

The session travels in an HttpOnly cookie rather than a bearer token so that
``EventSource`` - which cannot set request headers - keeps working for the
chat progress stream.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from backend.api.errors import ApiError
from backend.config import settings
from backend.db import dao

logger = logging.getLogger(__name__)

GUEST_PREFIX = "guest:"
USER_PREFIX = "user:"
_SALT = "jdm-session-v1"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.signing_key, salt=_SALT)


def is_guest(owner: str) -> bool:
    return owner.startswith(GUEST_PREFIX)


def _valid(owner: object) -> bool:
    return isinstance(owner, str) and (
        owner.startswith(GUEST_PREFIX) or owner.startswith(USER_PREFIX)
    )


def _read_session(request: Request) -> dict:
    """Decode the request's cookie, or an empty session if it is not intact.

    A tampered or stale-secret cookie is treated as absent rather than as an
    error: the visitor simply becomes a new guest.
    """
    raw = request.cookies.get(settings.session_cookie)
    if not raw:
        return {}
    try:
        data = _serializer().loads(raw)
    except BadSignature:
        return {}
    return data if isinstance(data, dict) else {}


def _read_cookie(request: Request) -> str | None:
    owner = _read_session(request).get("owner")
    return owner if _valid(owner) else None


def prior_guest(request: Request) -> str | None:
    """The guest this session came from, remembered across a sign-in."""
    guest = _read_session(request).get("guest")
    return guest if _valid(guest) and guest.startswith(GUEST_PREFIX) else None


def issue(response: Response, owner: str, guest: str | None = None) -> None:
    """Attach a signed session cookie for `owner` to the response.

    `guest` carries the anonymous identity the visitor had before signing in,
    so signing out returns them to their own work instead of stranding it
    under a session id nobody holds any more.
    """
    payload: dict[str, str] = {"owner": owner}
    if guest and guest != owner:
        payload["guest"] = guest
    token = _serializer().dumps(payload)
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.guest_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        # The dev stack is plain HTTP on localhost, where Secure would stop the
        # cookie being stored at all. Behind TLS this should be True.
        secure=False,
        path="/",
    )


def clear(response: Response) -> None:
    response.delete_cookie(settings.session_cookie, path="/")


async def get_owner(request: Request, response: Response) -> str:
    """FastAPI dependency: the owner for this request.

    Mints a guest session on first contact, so an anonymous visitor can use the
    studio immediately with no sign-up step.
    """
    owner = _read_cookie(request)
    if owner is None:
        owner = GUEST_PREFIX + secrets.token_urlsafe(16)
        issue(response, owner)
    return owner


async def require_admin(request: Request, response: Response) -> str:
    owner = await get_owner(request, response)
    if is_guest(owner):
        raise ApiError("UNAUTHORIZED", "Sign in to use this.", 401)
    if await dao.get_user(owner) is None:
        # The account was removed, or the signing secret changed identities.
        raise ApiError("UNAUTHORIZED", "Session is no longer valid.", 401)
    return owner
