"""Sign-in for the single admin account.

Guests need no endpoint here: a session is minted for them on their first
request by ``auth.get_owner``. These routes exist only to move an existing
session between guest and admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from backend import auth
from backend.api.errors import ApiError
from backend.config import settings
from backend.db import dao
from backend.models.api import LoginRequest, SessionResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=SessionResponse)
async def me(owner: str = Depends(auth.get_owner)) -> SessionResponse:
    """Who the caller is. Drives the sign-in / sign-out control in the UI."""
    if auth.is_guest(owner):
        return SessionResponse(
            mode="guest", login_enabled=bool(settings.admin_password)
        )
    user = await dao.get_user(owner)
    if user is None:
        return SessionResponse(
            mode="guest", login_enabled=bool(settings.admin_password)
        )
    return SessionResponse(mode="admin", email=user["email"], login_enabled=True)


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest, request: Request, response: Response
) -> SessionResponse:
    if not settings.admin_password:
        raise ApiError(
            "LOGIN_DISABLED",
            "No admin account is configured. Set ADMIN_PASSWORD in backend/.env.",
            503,
        )
    user = await dao.verify_user(body.email, body.password)
    if user is None:
        # Deliberately does not say which half was wrong.
        raise ApiError("INVALID_CREDENTIALS", "Incorrect email or password.", 401)
    # Remember the guest they were, so signing out does not orphan that work.
    current = auth._read_cookie(request)
    guest = current if current and auth.is_guest(current) else auth.prior_guest(request)
    auth.issue(response, user["id"], guest=guest)
    return SessionResponse(mode="admin", email=user["email"], login_enabled=True)


@router.post("/logout", response_model=SessionResponse)
async def logout(request: Request, response: Response) -> SessionResponse:
    """Sign out, returning to the guest identity this session came from.

    Anything the admin owns stays in the database; only the cookie changes.
    Restoring the previous guest matters because their policies are keyed to
    that id - minting a fresh guest would silently hide them.
    """
    guest = auth.prior_guest(request)
    if guest:
        auth.issue(response, guest)
    else:
        auth.clear(response)
    return SessionResponse(mode="guest", login_enabled=bool(settings.admin_password))
