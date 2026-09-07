"""Auth tối giản: 1 mật khẩu trong env + signed cookie. Không user table, không JWT."""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

COOKIE_NAME = "session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 ngày

_signer = URLSafeTimedSerializer(settings.session_secret, salt="auth")
_csrf_signer = URLSafeTimedSerializer(settings.session_secret, salt="csrf")


def check_password(candidate: str) -> bool:
    # compare_digest: so sánh thời gian hằng định, tránh timing attack
    return secrets.compare_digest(candidate, settings.app_password)


def issue(response, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _signer.dumps("ok"),
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear(response) -> None:
    response.delete_cookie(COOKIE_NAME)


def is_logged_in(request: Request) -> bool:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return False
    try:
        return _signer.loads(raw, max_age=MAX_AGE) == "ok"
    except (BadSignature, Exception):
        return False


def csrf_token() -> str:
    return _csrf_signer.dumps("f")


def csrf_ok(token: str | None) -> bool:
    if not token:
        return False
    try:
        _csrf_signer.loads(token, max_age=MAX_AGE)
        return True
    except Exception:
        return False


class RequireLogin:
    """Dependency. Chuyển hướng về /login nếu chưa đăng nhập."""

    def __call__(self, request: Request) -> None:
        if not is_logged_in(request):
            raise _Redirect()


class _Redirect(Exception):
    pass


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)
