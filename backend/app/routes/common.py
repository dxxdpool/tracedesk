import uuid
from typing import Annotated, TypeVar

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import Base, Investigation
from app.services.workspace import Workspace, WorkspaceError, workspace_repo_dir

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0)]

ModelT = TypeVar("ModelT", bound=Base)


def workspace_of(investigation: Investigation) -> Workspace:
    if investigation.repo_sha is None:
        raise ApiError(422, "investigation_not_pinned", "This investigation has no cloned repo at a pinned SHA")
    return Workspace(workspace_repo_dir(settings.workspaces_dir, investigation.id), investigation.repo_sha)


def workspace_api_error(exc: WorkspaceError, not_found_status: int = 404) -> ApiError:
    """422 for a rejected repo, path or range; a missing file is 404 on reads, 422 when cited as evidence."""
    status_code = not_found_status if exc.reason_code == "file_not_found" else 422
    return ApiError(status_code, exc.reason_code, exc.message)


async def get_or_404(session: AsyncSession, model: type[ModelT], row_id: uuid.UUID, reason_code: str) -> ModelT:
    row = await session.get(model, row_id)
    if row is None:
        raise ApiError(404, reason_code, f"No {model.__tablename__} row with id {row_id}")
    return row
