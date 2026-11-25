"""Embedding service (sync/blocking: call it from `def` routes or the threadpool/worker).

The model is loaded lazily and cached, so importing this module and starting the server stay cheap.
One model name + dimension is configured; the vector store refuses vectors of any other dimension.
"""
import logging
import threading
from typing import Callable, Optional, Protocol, Sequence

from app.core.exceptions import ServiceUnavailable

logger = logging.getLogger("uvicorn.error")

# Models that need instruction prefixes (matched on a lowercase substring of the model name).
MAX_SEQ_LENGTH = 512
_PREFIXES = {"e5": ("query: ", "passage: "), "bge-m3": ("", "")}


class Embedder(Protocol):
    model_name: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def _prefixes(model_name: str):
    name = model_name.lower()
    for key, pair in _PREFIXES.items():
        if key in name:
            return pair
    return "", ""


class EmbeddingService:
    def __init__(self, model_name: str, dimension: int, batch_size: int = 32,
                 model_factory: Optional[Callable[[str], object]] = None):
        self.model_name, self.dimension, self.batch_size = model_name, dimension, batch_size
        self._factory = model_factory
        self._model = None
        self._lock = threading.Lock()
        self._encode_lock = threading.Lock()  # one encode at a time: bounds CPU/RAM on a small host
        self._query_prefix, self._doc_prefix = _prefixes(model_name)

    def _load(self):
        with self._lock:
            if self._model is None:
                if self._factory:
                    model = self._factory(self.model_name)
                else:
                    from sentence_transformers import SentenceTransformer  # lazy: torch is heavy
                    model = SentenceTransformer(self.model_name, device="cpu")
                real = model.get_sentence_embedding_dimension()
                if real != self.dimension:  # never let a wrong EMBEDDING_DIMENSION create a mixed index
                    logger.error("Embedding dimension mismatch: model=%s config=%s", real, self.dimension)
                    raise ServiceUnavailable("Embedding model configuration mismatch")
                if hasattr(model, "max_seq_length") and model.max_seq_length < MAX_SEQ_LENGTH:
                    model.max_seq_length = MAX_SEQ_LENGTH  # default 128 tokens would truncate ~1000-char chunks
                logger.info("Embedding model %s loaded (dim=%s, max_seq_length=%s)", self.model_name, real,
                            getattr(model, "max_seq_length", "n/a"))
                self._model = model
            return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        with self._encode_lock:
            vectors = model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                   show_progress_bar=False)
        return [[float(x) for x in v] for v in vectors]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        texts = [self._doc_prefix + t for t in texts]
        for i in range(0, len(texts), self.batch_size):  # bounded memory for large PDFs
            out.extend(self._encode(texts[i:i + self.batch_size]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self._query_prefix + text])[0]
