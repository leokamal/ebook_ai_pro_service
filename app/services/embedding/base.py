"""The strategy interface every embedding provider implements, and the errors callers may see.

Sync/blocking: call it from `def` routes or the threadpool/worker.
"""
from typing import Protocol, Sequence

from app.core.exceptions import AppError


class Embedder(Protocol):
    model_name: str  # recorded in the Chroma collection: vectors of two models are never mixed
    dimension: int

    @property
    def loaded(self) -> bool: ...
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


# Deliberately NOT ServiceUnavailable subclasses: the job runner treats that as permanent, these are retried.
class EmbeddingQuotaError(AppError):
    def __init__(self):
        super().__init__("embedding_quota_exceeded", "The AI service quota is exhausted; try again later", 429,
                         {"Retry-After": "60"})


class EmbeddingUnavailable(AppError):
    def __init__(self):
        super().__init__("embedding_unavailable", "The AI service is temporarily unavailable", 503)
