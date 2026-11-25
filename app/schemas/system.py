from typing import Optional

from pydantic import BaseModel


class VectorIndexHealth(BaseModel):
    state: str  # not_initialized | closed | ready | needs_reindex | error
    collection: Optional[str] = None
    embedding_model: Optional[str] = None
    chunks: Optional[int] = None


class HealthResponse(BaseModel):
    status: str  # ok | unconfigured
    environment: str
    embedding_model: str
    embedding_loaded: bool
    vector_index: VectorIndexHealth
    worker: str = "stopped"
