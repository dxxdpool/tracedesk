"""Request (Create) and response (Read) models.

Create models forbid unknown fields, so a client cannot sneak in `status`, `id` or other
server-owned values: statuses only change through POST /hypotheses/{id}/transition.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import (
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
    """Snippet and repo_sha are not accepted: the server captures them from the pinned commit (Day 2)."""

    investigation_id: uuid.UUID
    hypothesis_id: uuid.UUID | None = None
    experiment_id: uuid.UUID | None = None
    kind: EvidenceKind
    source_type: EvidenceSource
    file_path: FilePath | None = None
    line_start: LineNumber | None = None
    line_end: LineNumber | None = None
    note: NonEmptyText | None = None

    @model_validator(mode="after")
    def check_line_range(self) -> Self:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start and line_end must be given together")
        if self.line_start is not None and self.line_end is not None and self.line_start > self.line_end:
            raise ValueError("line_start must be <= line_end")
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
    note: str | None
    repo_sha: str | None
    created_by: str
    created_at: datetime


class ExperimentCreate(CreateModel):
    """The investigation is taken from the hypothesis, so the two can never disagree."""

    hypothesis_id: uuid.UUID
    command: NonEmptyText
    why: NonEmptyText


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
