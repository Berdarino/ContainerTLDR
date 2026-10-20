import hashlib
import hmac
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

OPEN = ("/login", "/static/")


def session_key(data_dir: Path, password: str) -> str:
    """Signs the session cookie. The password is part of it, so changing the password logs everyone out."""
    path = data_dir / "session.key"
    if not path.exists():
        path.touch(mode=0o600)
        path.write_text(secrets.token_hex(32))
    return hmac.new(path.read_text().encode(), password.encode(), "sha256").hexdigest()


def same_password(attempt: str, password: str) -> bool:
    # Hashed first, so the comparison takes as long whatever the attempt's length
    return hmac.compare_digest(hashlib.sha256(attempt.encode()).digest(), hashlib.sha256(password.encode()).digest())


class RequireLogin(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.session.get("login") or request.url.path.startswith(OPEN):
            return await call_next(request)
        return RedirectResponse("/login", status_code=303)


def page_context(request: Request) -> dict:
    """What every template gets: whether someone is logged in, and the token their forms post back."""
    if "session" not in request.scope:  # no password set, so no login either
        return {}
    csrf = request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return {"csrf": csrf, "logged_in": bool(request.session.get("login"))}


async def posted(request: Request) -> FormData:
    """The posted form, if it came from one of our own pages."""
    form = await request.form()
    origin = request.headers.get("origin") or request.headers.get("referer")
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}  # behind a reverse proxy
    token = request.session.get("csrf", "").encode()
    sent = str(form.get("csrf", "")).encode()
    if (origin and urlsplit(origin).netloc not in hosts) or not token or not hmac.compare_digest(sent, token):
        raise HTTPException(403, "This form came from another site or has expired. Reload the page and try again.")
    return form
