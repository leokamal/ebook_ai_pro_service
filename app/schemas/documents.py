from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

DocStatus = Literal["uploaded", "queued", "processing", "completed", "failed", "deleting"]


class DocumentCreate(BaseModel):
    """No user_id and no file_path here on purpose: identity comes from the JWT, the path is derived."""
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    title: str = Field(min_length=1, max_length=300)
    file_name: str = Field(min_length=1, max_length=255)
    language: Optional[str] = Field(default=None, max_length=16)
    auto_process: bool = True


class DocumentOut(BaseModel):
    id: UUID
    title: str
    file_name: str
    file_path: str
    file_type: str
    file_size: int
    page_count: Optional[int] = None
    language: Optional[str] = None
    status: DocStatus
    index_state: str
    embedding_model: Optional[str] = None
    chunk_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    shared: bool = False  # added from another user by document id (the content ids are not exposed)

    @model_validator(mode="before")
    @classmethod
    def _derive_shared(cls, data):
        if isinstance(data, dict):
            owner = data.get("content_owner_id")
            data = {**data, "shared": bool(owner) and owner != data.get("user_id")}
        return data


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: Optional[str] = None


class DocumentStatusOut(BaseModel):
    status: DocStatus
    index_state: str
    progress: int = 0
    attempts: int = 0
    error_message: Optional[str] = None
    page_count: Optional[int] = None
    chunk_count: int = 0


class JobAccepted(BaseModel):
    document_id: UUID
    status: DocStatus
    job_id: Optional[UUID] = None


class MaintenanceResult(BaseModel):
    reindex_queued: int = 0
    checked: Optional[int] = None
