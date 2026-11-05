import asyncio
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.config import settings
from app.db import SessionDep
from app.errors import ApiError
from app.models import Investigation
from app.routes.common import get_or_404, workspace_api_error, workspace_of
from app.schemas import InvestigationCreate, InvestigationRead, RepoFileLines, RepoFiles, RepoTree
from app.services import workspace
from app.services.workspace import MAX_RANGE_LINES, BlameLine, Cloner, CommitInfo, WorkspaceError

router = APIRouter(prefix="/investigations", tags=["investigations"])

LineQuery = Annotated[int | None, Query(ge=1)]
MAX_CONCURRENT_CLONES = 2
# Bounds disk and network use when several investigations are created at once.
clone_slots = asyncio.Semaphore(MAX_CONCURRENT_CLONES)


def get_cloner() -> Cloner:
    """The git clone step; tests override it to clone a local fixture instead of GitHub."""
    return workspace.clone


def host_workspace_path(investigation_id: uuid.UUID) -> str:
    """The clone's path as agents on the host see it (differs from the API's path inside compose)."""
    if settings.workspaces_host_dir:
        base = settings.workspaces_host_dir
        joiner = PureWindowsPath if "\\" in base or ":" in base else PurePosixPath
        return str(joiner(base, str(investigation_id), "repo"))
    return str(workspace.workspace_repo_dir(settings.workspaces_dir, investigation_id))


def to_read(investigation: Investigation) -> InvestigationRead:
    read = InvestigationRead.model_validate(investigation)
    if investigation.repo_sha is not None:
        read.workspace_path = host_workspace_path(investigation.id)
    return read


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvestigationRead)
async def create_investigation(
    body: InvestigationCreate, session: SessionDep, cloner: Annotated[Cloner, Depends(get_cloner)]
) -> InvestigationRead:
    """Validate the repo, clone it into workspaces/<id>/repo and pin the exact HEAD SHA."""
    try:
        source = workspace.parse_repo_url(
            body.repo_url, allow_local=settings.allow_local_repos, local_dir=settings.local_repos_dir
        )
    except WorkspaceError as exc:
        raise workspace_api_error(exc) from exc

    # The id names the workspace folder, so it is chosen before the clone. No transaction is open
    # while git runs; the row is written only once the clone has succeeded.
    investigation_id = uuid.uuid4()
    repo_dir = workspace.workspace_repo_dir(settings.workspaces_dir, investigation_id)
    try:
        async with clone_slots:
            repo_sha = await asyncio.to_thread(cloner, source, repo_dir)
    except WorkspaceError as exc:
        await asyncio.to_thread(workspace.remove_tree, repo_dir.parent)
        raise workspace_api_error(exc) from exc

    investigation = Investigation(id=investigation_id, repo_sha=repo_sha, **body.model_dump())
    session.add(investigation)
    try:
        await session.commit()
    except BaseException:
        await asyncio.to_thread(workspace.remove_tree, repo_dir.parent)
        raise
    await session.refresh(investigation)
    return to_read(investigation)


@router.get("/{investigation_id}", response_model=InvestigationRead)
async def get_investigation(investigation_id: uuid.UUID, session: SessionDep) -> InvestigationRead:
    return to_read(await get_or_404(session, Investigation, investigation_id, "investigation_not_found"))


# --- read-only views of the pinned commit (debugging, and a way to check a citation first) ----


@router.get("/{investigation_id}/files", response_model=RepoFiles)
async def list_repo_files(investigation_id: uuid.UUID, session: SessionDep) -> RepoFiles:
    ws = workspace_of(await get_or_404(session, Investigation, investigation_id, "investigation_not_found"))
    try:
        files, truncated = await asyncio.to_thread(workspace.list_files, ws)
    except WorkspaceError as exc:
        raise workspace_api_error(exc) from exc
    return RepoFiles(repo_sha=ws.sha, files=files, truncated=truncated)


@router.get("/{investigation_id}/tree", response_model=RepoTree)
async def repo_tree(investigation_id: uuid.UUID, session: SessionDep) -> RepoTree:
    """Every file at the pinned commit with its blob id; `st experiment run` checks the workspace against it."""
    ws = workspace_of(await get_or_404(session, Investigation, investigation_id, "investigation_not_found"))
    try:
        entries, truncated = await asyncio.to_thread(workspace.tree_entries, ws)
    except WorkspaceError as exc:
        raise workspace_api_error(exc) from exc
    return RepoTree(repo_sha=ws.sha, entries=entries, truncated=truncated)


@router.get("/{investigation_id}/file", response_model=RepoFileLines)
async def read_repo_file(
    investigation_id: uuid.UUID,
    session: SessionDep,
    path: Annotated[str, Query(min_length=1)],
    start: LineQuery = None,
    end: LineQuery = None,
) -> RepoFileLines:
    """Lines of a file at the pinned SHA; without start/end, the first MAX_RANGE_LINES lines."""
    ws = workspace_of(await get_or_404(session, Investigation, investigation_id, "investigation_not_found"))
    try:
        relative, lines = await asyncio.to_thread(workspace.read_file, ws, path)
        first = start or 1
        last = end or min(len(lines), first + MAX_RANGE_LINES - 1)
        if not lines and start is None and end is None:
            return RepoFileLines(repo_sha=ws.sha, path=relative, line_start=0, line_end=0, line_count=0, lines=[])
        workspace.check_range(relative, len(lines), first, last)
    except WorkspaceError as exc:
        raise workspace_api_error(exc) from exc
    return RepoFileLines(
        repo_sha=ws.sha, path=relative, line_start=first, line_end=last,
        line_count=len(lines), lines=lines[first - 1 : last],
    )  # fmt: skip


@router.get("/{investigation_id}/log", response_model=list[CommitInfo])
async def repo_log(investigation_id: uuid.UUID, session: SessionDep, path: str | None = None) -> list[CommitInfo]:
    ws = workspace_of(await get_or_404(session, Investigation, investigation_id, "investigation_not_found"))
    try:
        return await asyncio.to_thread(workspace.git_log, ws, path)
    except WorkspaceError as exc:
        raise workspace_api_error(exc) from exc


@router.get("/{investigation_id}/blame", response_model=list[BlameLine])
async def repo_blame(
    investigation_id: uuid.UUID,
    session: SessionDep,
    path: Annotated[str, Query(min_length=1)],
    start: Annotated[int, Query(ge=1)],
    end: Annotated[int, Query(ge=1)],
) -> list[BlameLine]:
    ws = workspace_of(await get_or_404(session, Investigation, investigation_id, "investigation_not_found"))
    if end < start:
        raise ApiError(422, "lines_invalid", "start must be <= end")
    try:
        return await asyncio.to_thread(workspace.git_blame, ws, path, start, end)
    except WorkspaceError as exc:
        raise workspace_api_error(exc) from exc
