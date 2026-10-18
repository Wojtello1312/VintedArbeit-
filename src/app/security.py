"""Shared constant-time bearer authentication for operational REST endpoints."""

import secrets
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr


def bearer_auth(token: SecretStr | None) -> Callable[..., Awaitable[None]]:
    bearer = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if token is None:
            raise HTTPException(503, "API authentication is not configured")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), token.get_secret_value().encode()
        ):
            raise HTTPException(
                401, "Invalid API credentials", headers={"WWW-Authenticate": "Bearer"}
            )

    return authorize
