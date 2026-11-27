"""How each Gemini embedding model is called: one strategy per model, picked by `GEMINI_EMBEDDING_MODEL`.

`GeminiEmbedder` does the HTTP work (batches, retries, errors) and asks the strategy only for what differs.
A new Gemini model = one class here + one entry in `GEMINI_MODELS`.
"""
import math
from typing import Optional, Protocol


class GeminiModel(Protocol):
    def query_item(self, text: str) -> dict:
        """Fields of one `batchEmbedContents` request (besides model and dimension) for a search query."""

    def document_item(self, text: str) -> dict:
        """The same for a document chunk."""

    def finish(self, vector: list[float]) -> list[float]:
        """Post-processing of one returned vector."""


def _content(text: str) -> dict:
    return {"content": {"parts": [{"text": text}]}}


class Embedding2:
    """`gemini-embedding-2`: no `taskType` (the task is a text prefix); vectors come back normalised."""

    def query_item(self, text: str) -> dict:
        return _content("task: search result | query: " + text)

    def document_item(self, text: str) -> dict:
        return _content("title: none | text: " + text)

    def finish(self, vector: list[float]) -> list[float]:
        return vector


class Embedding001:
    """`gemini-embedding-001`: takes `taskType`; truncated vectors must be normalised by the caller."""

    def query_item(self, text: str) -> dict:
        return {**_content(text), "taskType": "RETRIEVAL_QUERY"}

    def document_item(self, text: str) -> dict:
        return {**_content(text), "taskType": "RETRIEVAL_DOCUMENT"}

    def finish(self, vector: list[float]) -> list[float]:
        norm = math.hypot(*vector) or 1.0
        return [x / norm for x in vector]


# model name prefix -> strategy (prefixes also match suffixed releases such as "...-preview")
GEMINI_MODELS: dict[str, type] = {"gemini-embedding-2": Embedding2, "gemini-embedding-001": Embedding001}


def create_gemini_model(model_name: str) -> Optional[GeminiModel]:
    """None for an unknown name: it is refused rather than guessed at."""
    return next((cls() for prefix, cls in GEMINI_MODELS.items() if model_name.startswith(prefix)), None)
