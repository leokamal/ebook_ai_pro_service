from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Usage(BaseModel):
    documents: int
    storage_bytes: int
    storage_limit_bytes: int
    objects: int
    object_limit: int


class MeResponse(BaseModel):
    id: UUID
    generated_key: Optional[UUID] = None
    email: Optional[str] = None
    name: Optional[str] = None  # the user's chosen display name
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None  # null when the user chose the default picture
    avatar_hidden: bool = False
    country: Optional[str] = None
    gender: Optional[str] = None
    providers: list[str]
    usage: Usage


class AccountDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["DELETE"]
