"""Embedding strategies and their factory (offline: fake HTTP client)."""
import pytest

from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailable
from app.services.embedding import (EmbeddingQuotaError, EmbeddingUnavailable, GeminiEmbedder, HuggingFaceEmbedder,
                                    create_embedder)

MODEL, DIM = "gemini-embedding-2", 4


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeHttp:
    """Plays `script` first (status codes or exceptions), then answers 200."""

    def __init__(self, *script, dim=DIM, drop=0):
        self.script, self.dim, self.drop, self.calls = list(script), dim, drop, []

    def post(self, url, json, headers):
        self.calls.append((url, json, headers))
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return Resp(step)
        n = len(json["requests"]) - self.drop
        return Resp(200, {"embeddings": [{"values": [float(i)] * self.dim} for i in range(n)]})


def make(http, batch=2, key="secret", model=MODEL):
    waits = []
    return GeminiEmbedder(key, model, DIM, batch, client_factory=lambda: http, sleep=waits.append), waits


def test_batches_prefixes_and_request_shape():
    http = FakeHttp()
    emb, _ = make(http)
    assert not emb.loaded
    assert len(emb.embed_documents(["a", "b", "c"])) == 3
    emb.embed_query("q")
    assert [len(c[1]["requests"]) for c in http.calls] == [2, 1, 1]
    url, body, headers = http.calls[0]
    assert url.endswith("/models/gemini-embedding-2:batchEmbedContents") and "secret" not in url
    assert headers == {"x-goog-api-key": "secret"}
    assert body["requests"][0] == {"model": "models/gemini-embedding-2", "outputDimensionality": DIM,
                                   "content": {"parts": [{"text": "title: none | text: a"}]}}
    assert http.calls[2][1]["requests"][0]["content"]["parts"][0]["text"] == "task: search result | query: q"


def test_ingestion_waits_out_quota_and_server_errors():
    emb, waits = make(FakeHttp(429, 503, ConnectionError()))
    assert len(emb.embed_documents(["a"])) == 1
    assert waits == [5, 15, 30]


def test_quota_gives_up_and_a_query_never_waits_for_it():
    emb, waits = make(FakeHttp(*[429] * 9))
    with pytest.raises(EmbeddingQuotaError):
        emb.embed_documents(["a"])
    assert waits == [5, 15, 30, 60]
    emb, waits = make(FakeHttp(429))
    with pytest.raises(EmbeddingQuotaError):
        emb.embed_query("q")
    assert waits == []


def test_client_errors_are_not_retried():
    emb, waits = make(FakeHttp(400))
    with pytest.raises(EmbeddingUnavailable):
        emb.embed_documents(["a"])
    assert waits == []


@pytest.mark.parametrize("http_kw,emb_kw,error", [
    ({"dim": DIM + 1}, {}, ServiceUnavailable),
    ({"drop": 1}, {}, EmbeddingUnavailable),
    ({}, {"key": ""}, ServiceUnavailable),
    ({}, {"model": "gemini-embedding-1"}, ServiceUnavailable),
])
def test_bad_configuration_or_response_is_refused(http_kw, emb_kw, error):
    emb, _ = make(FakeHttp(**http_kw), **emb_kw)
    with pytest.raises(error):
        emb.embed_documents(["a"])


def test_transient_embedding_errors_are_retried_by_the_job_runner():
    # the runner treats ServiceUnavailable as permanent: these must not be subclasses of it
    assert not issubclass(EmbeddingUnavailable, ServiceUnavailable)
    assert not issubclass(EmbeddingQuotaError, ServiceUnavailable)


def test_factory_picks_the_strategy():
    assert isinstance(create_embedder("gemini", MODEL, DIM, api_key="k"), GeminiEmbedder)
    assert isinstance(create_embedder("huggingface", "m", DIM), HuggingFaceEmbedder)
    with pytest.raises(ServiceUnavailable):
        create_embedder("nope", "m", DIM)


def test_switching_provider_is_one_variable(monkeypatch):
    for var in ("EMBEDDING_PROVIDER", "GEMINI_EMBEDDING_MODEL", "GEMINI_EMBEDDING_DIMENSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "local-model")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "384")
    s = get_settings()
    assert (s.embedding_provider, s.embedding_model, s.embedding_dimension) == ("gemini", MODEL, 768)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "HuggingFace")
    s = get_settings()
    assert (s.embedding_provider, s.embedding_model, s.embedding_dimension) == ("huggingface", "local-model", 384)


def test_gemini_001_uses_task_type_and_normalises():
    bodies = []

    class Client:
        def post(self, url, json, headers):
            bodies.append(json)

            class R:
                status_code = 200

                def json(self):
                    return {"embeddings": [{"values": [3.0, 4.0]}]}

            return R()

    e = GeminiEmbedder("k", "gemini-embedding-001", 2, client_factory=Client)
    assert e.embed_query("q") == pytest.approx([0.6, 0.8])
    e.embed_documents(["d"])
    sent = [b["requests"][0] for b in bodies]
    assert [(r["taskType"], r["content"]["parts"][0]["text"]) for r in sent] == [
        ("RETRIEVAL_QUERY", "q"), ("RETRIEVAL_DOCUMENT", "d")]
