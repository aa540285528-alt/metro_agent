from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _PublicSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class DraftListItem(_PublicSchema):
    id: str
    status: str
    original_filename: str
    package_sha256: str
    package_size_bytes: int
    actor_username: str
    created_at: datetime
    updated_at: datetime


class DraftDetail(DraftListItem):
    validation_report: dict[str, Any] | None


class JobStatus(_PublicSchema):
    id: str
    kind: str
    status: str
    draft_id: str | None
    release_build_id: str | None
    failure_summary: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    lease_expires_at: datetime | None


class ReleaseListItem(_PublicSchema):
    build_id: str
    document_count: int
    status: str
    published_at: datetime
    artifact_sha256: str
    source_manifest_sha256: str
    draft_id: str
    validation_summary: dict[str, Any]


class ReleaseDetail(ReleaseListItem):
    pass


class AuditListItem(_PublicSchema):
    id: str
    action: str
    result: str
    actor_user_id: str
    actor_username: str
    draft_id: str | None
    job_id: str | None
    release_build_id: str | None
    reason_summary: str | None
    failure_summary: str | None
    occurred_at: datetime


class DraftSubmissionResult(_PublicSchema):
    draft_id: str
    job_id: str
    status: str


class _AdminMutationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyAdminMutation(_AdminMutationSchema):
    pass


class RollbackRequest(_AdminMutationSchema):
    reason: str = Field(min_length=1, max_length=500, strict=True)

    @field_validator("reason")
    @classmethod
    def require_non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value
