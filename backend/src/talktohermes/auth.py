from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import Settings

_bearer = HTTPBearer(auto_error=False)


def require_app_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.app_token.get_secret_value()
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
