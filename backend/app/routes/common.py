import uuid
from typing import Annotated, TypeVar

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Base

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0)]

ModelT = TypeVar("ModelT", bound=Base)


async def get_or_404(session: AsyncSession, model: type[ModelT], row_id: uuid.UUID, reason_code: str) -> ModelT:
    row = await session.get(model, row_id)
    if row is None:
        raise ApiError(404, reason_code, f"No {model.__tablename__} row with id {row_id}")
    return row
