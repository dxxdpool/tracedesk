"""Request (Create) and response (Read) models.

Create models forbid unknown fields, so a client cannot sneak in `status`, `id` or other
server-owned values: statuses only change through POST /hypotheses/{id}/transition.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import (
    FINISHED_EXPERIMENT_STATUSES,
    PATH_LENGTH,
    URL_LENGTH,
    EvidenceKind,
    EvidenceSource,
    ExperimentOutcome,
    ExperimentStatus,
    HypothesisStatus,
    InvestigationStatus,
)

TEXT_LENGTH = 20_000
ROOM_ID_LENGTH = 255
MAX_POSTGRES_INT = 2_147_483_647


def reject_nul(value: str) -> str:
    # Postgres text cannot store NUL; without this the insert fails with a 500.
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


def text_type(max_length: int) -> Any:
    """Non-blank, trimmed, length-capped text that Postgres can store."""
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length),
        AfterValidator(reject_nul),
    ]


NonEmptyText = text_type(TEXT_LENGTH)
RepoUrl = text_type(URL_LENGTH)
FilePath = text_type(PATH_LENGTH)
RoomId = text_type(ROOM_ID_LENGTH)
LineNumber = Annotated[int, Field(ge=1, le=MAX_POSTGRES_INT)]
CommitId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-fA-F]{4,40}$")]


class CreateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InvestigationCreate(CreateModel):
    repo_url: RepoUrl
    problem: NonEmptyText
    switch_room_id: RoomId | None = None


class InvestigationRead(ReadModel):
    id: uuid.UUID
    repo_url: str
    repo_sha: str | None
    problem: str
    status: InvestigationStatus
    switch_room_id: str | None
    created_at: datetime
    # Where agents on the host find the clone; filled in by the route, not stored.
    workspace_path: str | None = None


class RepoFiles(BaseModel):
    repo_sha: str
    files: list[str]
    truncated: bool


class RepoTreeEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    mode: str
    blob: str


class RepoTree(BaseModel):
    repo_sha: str
    entries: list[RepoTreeEntry]
    truncated: bool


class RepoFileLines(BaseModel):
    repo_sha: str
    path: str
    line_start: int
    line_end: int
    line_count: int
    lines: list[str]


class HypothesisCreate(CreateModel):
    investigation_id: uuid.UUID
    text: NonEmptyText


class HypothesisRead(ReadModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    text: str
    status: HypothesisStatus
    created_by: str
    created_at: datetime
    updated_at: datetime


class TransitionRequest(CreateModel):
    to: HypothesisStatus
    reason: NonEmptyText


class HypothesisEventRead(ReadModel):
    id: uuid.UUID
    hypothesis_id: uuid.UUID
    from_status: HypothesisStatus
    to_status: HypothesisStatus
    actor: str
    reason: str
    created_at: datetime


class EvidenceCreate(CreateModel):
    """Snippet and repo_sha are not accepted: the server captures them from the pinned commit.

    Shape per source_type: code_ref needs file_path + lines; git_history needs commit_sha (file_path
    optional, no lines); other sources carry no citation at all, so nothing unchecked looks cited.
    """

    investigation_id: uuid.UUID
    hypothesis_id: uuid.UUID | None = None
    experiment_id: uuid.UUID | None = None
    kind: EvidenceKind
    source_type: EvidenceSource
    file_path: FilePath | None = None
    line_start: LineNumber | None = None
    line_end: LineNumber | None = None
    commit_sha: CommitId | None = None
    note: NonEmptyText | None = None

    @model_validator(mode="after")
    def check_line_range(self) -> Self:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start and line_end must be given together")
        if self.line_start is not None and self.line_end is not None and self.line_start > self.line_end:
            raise ValueError("line_start must be <= line_end")
        return self

    @model_validator(mode="after")
    def check_citation_shape(self) -> Self:
        has_lines = self.line_start is not None
        if self.source_type == EvidenceSource.CODE_REF:
            if self.file_path is None or not has_lines or self.commit_sha is not None:
                raise ValueError("code_ref evidence needs file_path, line_start and line_end (and no commit_sha)")
        elif self.source_type == EvidenceSource.GIT_HISTORY:
            if self.commit_sha is None or has_lines:
                raise ValueError("git_history evidence needs commit_sha (file_path optional, no lines)")
        elif self.file_path is not None or has_lines or self.commit_sha is not None:
            raise ValueError(f"{self.source_type.value} evidence cannot cite files, lines or commits")
        return self


class EvidenceRead(ReadModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    hypothesis_id: uuid.UUID | None
    experiment_id: uuid.UUID | None
    kind: EvidenceKind
    source_type: EvidenceSource
    file_path: str | None
    line_start: int | None
    line_end: int | None
    snippet: str | None
    commit_sha: str | None
    note: str | None
    repo_sha: str | None
    created_by: str
    created_at: datetime


class ExperimentCreate(CreateModel):
    """The investigation is taken from the hypothesis, so the two can never disagree."""

    hypothesis_id: uuid.UUID
    command: NonEmptyText
    why: NonEmptyText


# The runner keeps 64 KB per stream and appends a truncation marker; this leaves room for it.
MAX_LOG_CHARS = 70_000
LogText = Annotated[str, StringConstraints(max_length=MAX_LOG_CHARS)]


class ExperimentResult(CreateModel):
    """Uploaded by `st experiment run` after the sandbox finishes."""

    status: ExperimentStatus
    exit_code: Annotated[int, Field(ge=-MAX_POSTGRES_INT, le=MAX_POSTGRES_INT)] | None = None
    duration_ms: Annotated[int, Field(ge=0, le=MAX_POSTGRES_INT)]
    stdout: LogText = ""
    stderr: LogText = ""

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.status not in FINISHED_EXPERIMENT_STATUSES:
            raise ValueError("status must be completed, failed or timed_out")
        if self.status == ExperimentStatus.COMPLETED and self.exit_code is None:
            raise ValueError("a completed experiment needs its exit_code")
        return self


class ExperimentConclusion(CreateModel):
    outcome: ExperimentOutcome


class ExperimentRead(ReadModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    hypothesis_id: uuid.UUID
    command: str
    why: str
    status: ExperimentStatus
    exit_code: int | None
    outcome: ExperimentOutcome | None
    stdout_path: str | None
    stderr_path: str | None
    duration_ms: int | None
    requested_by: str
    approved_by: str | None
    created_at: datetime
    finished_at: datetime | None
