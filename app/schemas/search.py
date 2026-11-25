from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchRequest(BaseModel):
    """Retrieval only (no LLM). Scope is explicit; the user comes from the token, never from the body."""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["single_document", "all_documents"]
    document_id: Optional[UUID] = None
    top_k: int = Field(default=6, ge=1, le=20)

    @model_validator(mode="after")
    def scope_is_consistent(self):
        if (self.mode == "single_document") != (self.document_id is not None):
            raise ValueError("document_id is required for single_document and forbidden for all_documents")
        return self


class SearchResult(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_title: str
    page_number: int
    score: float
    text: str


class SearchResponse(BaseModel):
    results: list[SearchResult]
    searched_documents: int
