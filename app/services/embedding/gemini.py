"""Gemini embedding strategy (REST `batchEmbedContents` over httpx; no local model).

`gemini-embedding-2` has no `taskType` field: the task goes into a text prefix, and truncated vectors are
already normalised by the API. Other Gemini embedding models differ on both points, so they are refused.
"""
import logging
import threading
import time
from typing import Callable, Optional, Sequence

from app.core.exceptions import ServiceUnavailable
from app.services.embedding.base import EmbeddingQuotaError, EmbeddingUnavailable

logger = logging.getLogger("uvicorn.error")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
SUPPORTED_PREFIX = "gemini-embedding-2"
MAX_BATCH = 100
QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: none | text: "
# Waits between attempts. Ingestion can sit out a per-minute quota window (waits + 5 x 30 s timeouts stay under
# the 300 s job lease); a user waiting on a search or chat gets one quick retry and never waits on a quota.
DOC_DELAYS = (5, 15, 30, 60)
QUERY_DELAYS = (1,)


class GeminiEmbedder:
    def __init__(self, api_key: str, model_name: str, dimension: int, batch_size: int = 32, timeout: float = 30.0,
                 client_factory: Optional[Callable[[], object]] = None, sleep: Callable[[float], None] = time.sleep):
        self.model_name, self.dimension = model_name, dimension
        self.batch_size = max(1, min(batch_size, MAX_BATCH))
        self.timeout = timeout
        self._api_key, self._factory, self._sleep = api_key, client_factory, sleep
        self._client = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._client is not None

    def _http(self):
        with self._lock:
            if self._client is None:
                if not self._api_key:
                    logger.error("GEMINI_API_KEY is not configured")
                    raise ServiceUnavailable("Embedding service not configured")
                if not self.model_name.startswith(SUPPORTED_PREFIX):
                    logger.error("Unsupported Gemini embedding model: %s", self.model_name)
                    raise ServiceUnavailable("Embedding model configuration mismatch")
                if self._factory:
                    self._client = self._factory()
                else:
                    import httpx
                    self._client = httpx.Client(timeout=self.timeout)
            return self._client

    def _request(self, texts: list[str], delays: tuple, retry_quota: bool) -> list[list[float]]:
        client = self._http()
        model = f"models/{self.model_name}"
        body = {"requests": [{"model": model, "content": {"parts": [{"text": t}]},
                              "outputDimensionality": self.dimension} for t in texts]}
        attempt = 0
        while True:
            quota = retry = False
            try:
                resp = client.post(f"{BASE_URL}/{model}:batchEmbedContents", json=body,
                                   headers={"x-goog-api-key": self._api_key})  # header, never the URL (logs)
                status = resp.status_code
                if status == 200:
                    return self._vectors(resp.json(), len(texts))
                quota = status == 429
                retry = (quota and retry_quota) or status >= 500
                logger.warning("Gemini embedding call failed: HTTP %s", status)
            except (EmbeddingUnavailable, ServiceUnavailable):
                raise
            except Exception as e:  # network / timeout / unreadable body
                retry = True
                logger.warning("Gemini embedding call failed: %s", type(e).__name__)
            if not retry or attempt >= len(delays):
                raise (EmbeddingQuotaError() if quota else EmbeddingUnavailable())
            self._sleep(delays[attempt])
            attempt += 1

    def _vectors(self, data: dict, expected: int) -> list[list[float]]:
        vectors = [[float(x) for x in e["values"]] for e in data["embeddings"]]
        if len(vectors) != expected:
            logger.error("Gemini returned %d embeddings for %d texts", len(vectors), expected)
            raise EmbeddingUnavailable()
        if any(len(v) != self.dimension for v in vectors):  # never let a wrong size reach the index
            logger.error("Gemini embedding dimension differs from the configured %s", self.dimension)
            raise ServiceUnavailable("Embedding model configuration mismatch")
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        texts = [DOC_PREFIX + t for t in texts]
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._request(texts[i:i + self.batch_size], DOC_DELAYS, retry_quota=True))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._request([QUERY_PREFIX + text], QUERY_DELAYS, retry_quota=False)[0]
