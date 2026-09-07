from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _PublicSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class ReleaseListItem(_PublicSchema):
    build_id: str
    document_count: int
    status: str
    published_at: datetime


class ReleaseDetail(ReleaseListItem):
    artifact_sha256: str
    source_manifest_sha256: str
    draft_id: str
    validation_summary: dict[str, Any]
