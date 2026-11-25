from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["single_document", "all_documents"]
    document_id: Optional[UUID] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class ConversationOut(BaseModel):
    id: UUID
    title: str
    mode: str
    document_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: Optional[str] = None


class SourceOut(BaseModel):
    document_id: Optional[UUID] = None
    document_title: Optional[str] = None
    page_number: int
    chunk_id: Optional[UUID] = None
    relevance_score: Optional[float] = None
    snippet: Optional[str] = None


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    token_count: int = 0
    created_at: datetime
    sources: list[SourceOut] = []


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: Optional[str] = None


class ChatRequest(BaseModel):
    """Scope is taken from the conversation; there is deliberately no user_id / document_id here."""
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    message: str = Field(min_length=1, max_length=4000)
    summarize: bool = False


class ChatResponse(BaseModel):
    message_id: UUID
    answer: str
    sources: list[SourceOut]
    usage: dict = {}
