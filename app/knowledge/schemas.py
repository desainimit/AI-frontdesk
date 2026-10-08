"""Public views never contain object storage keys or raw embeddings."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    source_type: str
    status: str
    active_version: int
    created_at: datetime


class VersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    checksum: str
    status: str
    error_code: str | None
    created_at: datetime


class SourceView(BaseModel):
    document_id: UUID
    version: int
    document_name: str
    section: str | None
    subsection: str | None
    page_number: int | None
    text: str
    distance: float


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=5, max_length=300)
    limit: int = Field(default=3, ge=1, le=5)


class KnowledgeResult(BaseModel):
    status: str
    sources: list[SourceView] = Field(default_factory=lambda: list[SourceView]())
