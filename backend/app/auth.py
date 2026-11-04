"""Demo-grade bearer auth: one shared token per role, set in the environment.

The role comes only from the token. X-Actor is a name recorded on rows, never a permission.
"""

import secrets
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.errors import ApiError
from app.state_machine import Role

bearer = HTTPBearer(auto_error=False)


def _role_for_token(token: str) -> Role | None:
    candidates = (
        (Role.AGENT, settings.agent_token),
        (Role.VALIDATOR, settings.validator_token),
        (Role.HUMAN, settings.approver_token),
    )
    for role, expected in candidates:
        # Constant-time comparison so the token cannot be guessed from response timing.
        if secrets.compare_digest(token.encode(), expected.get_secret_value().encode()):
            return role
    return None


async def get_role(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Role:
    if credentials is None:
        raise ApiError(401, "token_missing", "Send 'Authorization: Bearer <token>'")
    role = _role_for_token(credentials.credentials)
    if role is None:
        raise ApiError(401, "token_invalid", "The bearer token is not recognised")
    return role


CurrentRole = Annotated[Role, Depends(get_role)]
