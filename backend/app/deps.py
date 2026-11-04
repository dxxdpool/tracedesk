from typing import Annotated

from fastapi import Depends, Header

from app.errors import ApiError
from app.models import ACTOR_LENGTH


async def get_actor(x_actor: Annotated[str, Header(alias="X-Actor", max_length=ACTOR_LENGTH)]) -> str:
    """Who is acting (agent name or human). Recorded on every row, never trusted for security:
    roles come from the bearer token (Day 1 Part C)."""
    actor = x_actor.strip()
    if not actor or "\x00" in actor:
        raise ApiError(422, "actor_missing", "Send a non-blank X-Actor header naming who is acting")
    return actor


Actor = Annotated[str, Depends(get_actor)]
