import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import auth_store

security = HTTPBasic()

# No lockout at all previously meant unlimited password guesses against a
# real credential over the LAN (or wherever this ends up exposed once
# shared with other people) — simple in-memory per-IP throttle, reset on
# restart. Proportionate for a single-process home-lab tool; not trying to
# be a full auth service.
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 300
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def check_credentials(request: Request, credentials: HTTPBasicCredentials) -> str:
    """Shared logic behind require_login — split out so the root route can
    invoke it manually (after its own "is setup complete?" redirect check)
    instead of going through FastAPI's Depends() every time.
    """
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = _failed_attempts[client_ip]
    attempts[:] = [t for t in attempts if now - t < _WINDOW_SECONDS]
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts — try again in a few minutes",
        )

    if not auth_store.verify_admin(credentials.username, credentials.password):
        attempts.append(now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_login(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    return check_credentials(request, credentials)
