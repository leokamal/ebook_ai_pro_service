"""Embeddings: Strategy (one class per provider, same `Embedder` interface) + Factory (`create_embedder`).

Importing this package is cheap: no provider loads a model or opens a connection until first use.
To add a provider: write a class that satisfies `Embedder` and register a builder in `factory.PROVIDERS`.
"""
from app.services.embedding.base import Embedder, EmbeddingQuotaError, EmbeddingUnavailable
from app.services.embedding.factory import PROVIDERS, create_embedder
from app.services.embedding.gemini import GeminiEmbedder
from app.services.embedding.huggingface import HuggingFaceEmbedder

__all__ = ["Embedder", "EmbeddingQuotaError", "EmbeddingUnavailable", "PROVIDERS", "create_embedder",
           "GeminiEmbedder", "HuggingFaceEmbedder"]
