import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import install_error_handlers
from app.services.llm import GeminiService, LLMQuotaError
from app.services.rag_parts import not_found_message
from tests.pro.test_chat_api import ChatApi
from tests.pro.test_rag import A, B, Rig


def sse_events(resp):
    out = []
    for block in resp.text.split("\n\n"):
        if not block.strip():
            continue
        name = next(l[7:] for l in block.split("\n") if l.startswith("event: "))
        data = json.loads(next(l[6:] for l in block.split("\n") if l.startswith("data: ")))
        out.append((name, data))
    return out


@pytest.fixture
def api(monkeypatch):
    return ChatApi(monkeypatch)


def setup_conv(api, chunks=((4, "the cat sleeps all day"),), **doc_kw):
    d = api.rig.add_doc(A, "Pets", list(chunks), **doc_kw)
    return d, api.create_conv(mode="single_document", document_id=d).json()["id"]


def stream(api, cid, message="What do cats do?", **extra):
    return api.c.post("/api/v1/chat/stream", json={"conversation_id": cid, "message": message, **extra})


# ---------------------------------------------------------------- happy path
def test_stream_event_order_headers_and_persisted_answer(api):
    d, cid = setup_conv(api)
    api.llm.replies = ["Cats sleep a lot [S1]."]
    r = stream(api, cid)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no" and r.headers["cache-control"] == "no-cache"
    assert r.headers["x-request-id"]
    ev = sse_events(r)
    names = [n for n, _ in ev]
    assert names[0] == "meta" and names[-3:] == ["token", "sources", "done"][-3:] or names[-2:] == ["sources", "done"]
    assert names[:1] == ["meta"] and set(names[1:-2]) == {"token"} and names[-2:] == ["sources", "done"]
    tokens = "".join(d_["text"] for n, d_ in ev if n == "token")
    done = ev[-1][1]
    assert tokens == done["answer"] == "Cats sleep a lot [S1]."
    assert done["refused"] is False and done["usage"]["output_tokens"] == 7
    src = dict(ev)["sources"]["sources"][0]
    assert src["document_id"] == d and src["page_number"] == 4
    stored = [m for m in api.rig.tables["messages"] if m["role"] == "assistant"]
    assert len(stored) == 1 and stored[0]["content"] == tokens and stored[0]["id"] == done["message_id"]
    assert [m["role"] for m in api.rig.tables["messages"]] == ["user", "assistant"]
    assert len(api.rig.tables["message_sources"]) == 1


def test_stream_refusal_never_leaks_the_sentinel(api):
    _, cid = setup_conv(api)
    api.llm.replies = ["NO_ANSWER: Les documents ne contiennent pas cette information."]
    ev = sse_events(stream(api, cid, "Quel est le prix ?"))
    text = "".join(d["text"] for n, d in ev if n == "token")
    assert "NO_ANSWER" not in text and text.startswith("Les documents")
    done = ev[-1][1]
    assert done["refused"] is True and dict(ev)["sources"]["sources"] == [] and done["answer"] == text


def test_stream_uncited_answer_is_replaced_by_refusal_in_done(api):
    _, cid = setup_conv(api)
    api.llm.replies = ["Cats are great animals."]
    ev = sse_events(stream(api, cid))
    done = ev[-1][1]
    assert done["refused"] is True and done["answer"] == not_found_message("What do cats do?")
    assert [m["content"] for m in api.rig.tables["messages"] if m["role"] == "assistant"] == [done["answer"]]


def test_stream_with_nothing_to_search_refuses_without_calling_the_llm(api):
    cid = api.create_conv(mode="all_documents").json()["id"]
    ev = sse_events(stream(api, cid, "What is this?"))
    assert [n for n, _ in ev] == ["meta", "token", "sources", "done"] and api.llm.calls == []
    assert ev[-1][1]["refused"] is True


def test_short_answers_are_not_swallowed_by_the_refusal_filter(api):
    _, cid = setup_conv(api)
    api.llm.replies = ["NO"]            # a prefix of the sentinel, then the stream ends
    ev = sse_events(stream(api, cid))
    assert "".join(d["text"] for n, d in ev if n == "token") == "NO"


# ---------------------------------------------------------------- failures
def test_prestream_errors_are_normal_http_errors(api):
    d, cid = setup_conv(api)
    api.user = B
    r = stream(api, cid)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found" and r.json()["error"]["request_id"]
    api.user = A
    d2 = api.rig.add_doc(A, "Draft", [(1, "cat")], status="processing", index_state="none")
    c2 = api.create_conv(mode="single_document", document_id=d2).json()["id"]
    r = stream(api, c2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "document_not_indexed"
    assert stream(api, cid, message="").status_code == 422
    assert stream(api, cid, document_id=d).status_code == 422
    assert api.rig.tables["messages"] == []


def test_quota_error_before_first_token_is_an_error_event_and_stores_nothing(api):
    _, cid = setup_conv(api)
    api.llm.replies = [LLMQuotaError()]
    ev = sse_events(stream(api, cid))
    assert [n for n, _ in ev] == ["meta", "error"] and ev[-1][1]["code"] == "llm_quota_exceeded"
    assert api.rig.tables["messages"] == []


def test_failure_mid_stream_is_an_error_event_and_stores_nothing(api):
    _, cid = setup_conv(api)
    api.llm.replies = [("Cats sleep a lot", RuntimeError("secret internal detail"))]
    r = stream(api, cid)
    ev = sse_events(r)
    assert ev[-1][0] == "error" and ev[-1][1]["code"] == "internal_error"
    assert "secret" not in r.text and "done" not in [n for n, _ in ev]
    assert api.rig.tables["messages"] == []


def test_client_disconnect_stores_nothing():
    rig = Rig()
    d = rig.add_doc(A, "Pets", [(1, "cat facts")])
    c = rig.conv(A, document_id=d)
    from tests.pro.test_rag import FakeLLM
    o = rig.orch(A, FakeLLM("Cats are small and purr a lot [S1]."))
    gen = o.stream(o.prepare(c, "cats?"))
    assert next(gen)[0] == "meta" and next(gen)[0] == "token"
    gen.close()                                   # what Starlette does when the client goes away
    assert rig.tables["messages"] == [] and rig.tables["message_sources"] == []


def test_stream_is_rate_limited_with_the_chat_budget(api, monkeypatch):
    monkeypatch.setenv("CHAT_RATE_PER_MINUTE", "1")
    _, cid = setup_conv(api)
    api.llm.replies = ["a [S1]", "b [S1]"]
    assert stream(api, cid).status_code == 200
    r = stream(api, cid)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


# ---------------------------------------------------------------- Gemini streaming adapter
def test_gemini_stream_yields_text_then_usage(monkeypatch):
    class Chunk:
        def __init__(self, content, usage=None):
            self.content, self.usage_metadata = content, usage

    class Model:
        def stream(self, messages):
            yield Chunk("Hel")
            yield Chunk([{"text": "lo"}], {"input_tokens": 5, "output_tokens": 2})

    monkeypatch.setattr(GeminiService, "_to_lc", staticmethod(lambda s, m: [s, *m]))
    svc = GeminiService("k", "m", model_factory=lambda: Model())
    assert list(svc.stream("sys", [])) == ["Hel", "lo", {"input_tokens": 5, "output_tokens": 2}]


def test_gemini_stream_maps_errors(monkeypatch):
    class Model:
        def stream(self, messages):
            raise Exception("429 RESOURCE_EXHAUSTED")
            yield

    monkeypatch.setattr(GeminiService, "_to_lc", staticmethod(lambda s, m: [s, *m]))
    with pytest.raises(LLMQuotaError):
        list(GeminiService("k", "m", model_factory=lambda: Model()).stream("s", []))


# ---------------------------------------------------------------- request ids + error envelope
def test_request_id_is_echoed_validated_and_matches_error_body(api):
    r = api.c.get("/api/v1/conversations/not-a-uuid", headers={"X-Request-ID": "client-req-12345"})
    assert r.headers["x-request-id"] == "client-req-12345" and r.json()["error"]["request_id"] == "client-req-12345"
    r = api.c.get("/api/v1/conversations/not-a-uuid", headers={"X-Request-ID": "bad id\twith junk"})
    assert r.headers["x-request-id"] != "bad id\twith junk" and len(r.headers["x-request-id"]) == 32


def test_unexpected_errors_are_an_envelope_on_api_and_unchanged_on_legacy():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/v1/boom")
    def boom():
        raise RuntimeError("db password is hunter2")

    @app.get("/legacy/boom")
    def legacy_boom():
        raise RuntimeError("x")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/v1/boom")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error" and "hunter2" not in r.text
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]
    r = c.get("/legacy/boom")
    assert r.status_code == 500 and r.text == "Internal Server Error"


# ---------------------------------------------------------------- OpenAPI contract
@pytest.fixture(scope="module")
def spec():
    import main
    return TestClient(main.app).get("/openapi.json").json()


REQUIRED = [("get", "/api/v1/me"), ("delete", "/api/v1/me"), ("post", "/api/v1/documents"), ("get", "/api/v1/documents"), ("get", "/api/v1/documents/{document_id}"),
            ("delete", "/api/v1/documents/{document_id}"), ("post", "/api/v1/documents/{document_id}/process"),
            ("get", "/api/v1/documents/{document_id}/status"), ("post", "/api/v1/documents/{document_id}/retry"),
            ("post", "/api/v1/conversations"), ("get", "/api/v1/conversations"),
            ("get", "/api/v1/conversations/{conversation_id}"), ("patch", "/api/v1/conversations/{conversation_id}"),
            ("delete", "/api/v1/conversations/{conversation_id}"),
            ("get", "/api/v1/conversations/{conversation_id}/messages"), ("post", "/api/v1/chat"),
            ("post", "/api/v1/chat/stream"), ("post", "/api/v1/search"), ("get", "/api/v1/health")]


def test_openapi_contains_every_required_endpoint_and_nothing_else(spec):
    for method, path in REQUIRED:
        assert method in spec["paths"].get(path, {}), (method, path)
    assert not [p for p in spec["paths"] if not p.startswith("/api/v1")]   # Pro only


def test_openapi_security_errors_and_streaming_are_documented(spec):
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert "ErrorResponse" in spec["components"]["schemas"]
    for method, path in REQUIRED:
        op = spec["paths"][path][method]
        assert op.get("summary") or op.get("description") or op["operationId"], path
        if path != "/api/v1/health":
            assert any("HTTPBearer" in s for s in op.get("security", [])), path
            assert "401" in op["responses"], path
    assert "text/event-stream" in spec["paths"]["/api/v1/chat/stream"]["post"]["responses"]["200"]["content"]
    assert "security" not in spec["paths"]["/api/v1/health"]["get"]


def test_no_request_schema_accepts_a_client_supplied_identity(spec):
    schemas = spec["components"]["schemas"]
    for name in ("DocumentCreate", "ConversationCreate", "ConversationUpdate", "ChatRequest", "SearchRequest", "AccountDeleteRequest"):
        props = schemas[name]["properties"]
        assert "user_id" not in props and "file_path" not in props, name
        assert schemas[name].get("additionalProperties") is False, name


# ---------------------------------------------------------------- concurrency cap, deadline, auto-title
def test_concurrency_limiter_caps_releases_and_self_heals():
    from app.core.exceptions import AppError
    from app.core.ratelimit import ConcurrencyLimiter
    now = [0.0]
    lim = ConcurrencyLimiter(2, ttl=100, clock=lambda: now[0])
    t1, t2 = lim.acquire("u"), lim.acquire("u")
    with pytest.raises(AppError) as e:
        lim.acquire("u")
    assert e.value.status_code == 429 and e.value.code == "rate_limited"
    assert lim.acquire("other")                    # per user
    lim.release("u", t1)
    lim.release("u", t1)                           # idempotent
    t3 = lim.acquire("u")
    now[0] = 101                                   # leaked slots expire on their own
    assert lim.acquire("u") and lim.acquire("u")
    assert ConcurrencyLimiter(0).acquire("u") is None   # disabled


def test_stream_slots_are_released_on_success_and_on_prestream_errors(api, monkeypatch):
    monkeypatch.setenv("CHAT_MAX_CONCURRENT_STREAMS", "1")
    _, cid = setup_conv(api)
    api.llm.replies = ["a [S1]", "b [S1]", "c [S1]"]
    assert stream(api, cid).status_code == 200
    assert stream(api, cid).status_code == 200      # the first slot was released
    api.user = B
    assert stream(api, cid).status_code == 404      # failing prepare() must release too
    api.user = A
    assert stream(api, cid).status_code == 200
    from app.core.ratelimit import stream_limiter
    assert not stream_limiter()._slots


def test_stream_deadline_aborts_a_slow_generation_without_storing(api, monkeypatch):
    from tests.pro.test_rag import FakeLLM
    rig = Rig()
    d = rig.add_doc(A, "Pets", [(1, "cat facts")])
    c = rig.conv(A, document_id=d)
    o = rig.orch(A, FakeLLM("Cats are small [S1]."), llm_stream_deadline=-1)
    events = list(o.stream(o.prepare(c, "cats?")))
    assert events[-1][0] == "error" and events[-1][1]["code"] == "llm_unavailable"
    assert rig.tables["messages"] == []


def test_auto_title_does_not_need_the_user_scoped_client():
    from tests.pro.test_rag import FakeLLM
    rig = Rig()
    d = rig.add_doc(A, "Pets", [(1, "cat facts")])
    c = rig.conv(A, document_id=d)
    o = rig.orch(A, FakeLLM("Cats [S1]."))
    o.conversations.update_title = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("client closed"))
    o.answer(c, "What do cats do?")
    assert rig.tables["conversations"][0]["title"] == "What do cats do?"
    o2 = rig.orch(A, FakeLLM("Cats [S1]."))
    o2.answer(c, "second question")
    assert rig.tables["conversations"][0]["title"] == "What do cats do?"   # only while still the default
