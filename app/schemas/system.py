from typing import Optional

from pydantic import BaseModel


class VectorIndexHealth(BaseModel):
    state: str  # not_initialized | closed | ready | needs_reindex | error
    collection: Optional[str] = None
    embedding_model: Optional[str] = None
    chunks: Optional[int] = None


class HealthResponse(BaseModel):
    status: str  # ok | degraded | unconfigured
    database: str  # ok | unreachable | bad_anon_key | bad_service_key | schema_outdated | error | unconfigured
    embedding: str  # ok | no_api_key | not_installed | unknown_provider
    embedding_provider: str
    environment: str
    embedding_model: str
    embedding_loaded: bool
    vector_index: VectorIndexHealth
    worker: str = "stopped"
