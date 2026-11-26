"""Builds the configured embedding strategy (`EMBEDDING_PROVIDER`)."""
import logging
from typing import Callable

from app.core.exceptions import ServiceUnavailable
from app.services.embedding.base import Embedder
from app.services.embedding.gemini import GeminiEmbedder
from app.services.embedding.huggingface import HuggingFaceEmbedder

logger = logging.getLogger("uvicorn.error")

# name -> builder(model_name, dimension, batch_size, api_key)
PROVIDERS: dict[str, Callable[[str, int, int, str], Embedder]] = {
    "gemini": lambda model, dim, batch, key: GeminiEmbedder(key, model, dim, batch),
    "huggingface": lambda model, dim, batch, key: HuggingFaceEmbedder(model, dim, batch),
}


def create_embedder(provider: str, model_name: str, dimension: int, batch_size: int = 32,
                    api_key: str = "") -> Embedder:
    build = PROVIDERS.get(provider)
    if build is None:
        logger.error("Unknown EMBEDDING_PROVIDER %r (expected one of: %s)", provider, ", ".join(PROVIDERS))
        raise ServiceUnavailable("Embedding model configuration mismatch")
    return build(model_name, dimension, batch_size, api_key)
