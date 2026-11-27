"""`/api/v1/health` must say why a deployment cannot work, without leaking anything (offline)."""
import pytest
from fastapi.testclient import TestClient

import main
from app.core.config import get_settings
from app.services import readiness


@pytest.fixture
def env(monkeypatch):
    for name, value in {"SUPABASE_URL": "https://ref.supabase.co", "SUPABASE_ANON_KEY": "sb_publishable_x",
                        "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_x", "GEMINI_API_KEY": "k",
                        "EMBEDDING_PROVIDER": "gemini"}.items():
        monkeypatch.setenv(name, value)
    readiness._cache.clear()
    return monkeypatch


class R:
    def __init__(self, status):
        self.status_code = status


def responder(auth=200, rest=200):
    return lambda url, **_: R(auth if "/auth/" in url else rest)


@pytest.mark.parametrize("get,expected", [
    (responder(), "ok"), (responder(auth=401), "bad_anon_key"), (responder(rest=401), "bad_service_key"),
    (responder(rest=400), "schema_outdated"), (responder(rest=500), "error"),
])
def test_probe_codes(env, get, expected):
    assert readiness._probe(get_settings(), get) == expected


def test_unreachable_and_cached(env):
    calls = []

    def down(url, **_):
        calls.append(url)
        raise OSError("Name or service not known")

    env.setattr("httpx.get", down)
    s = get_settings()
    assert [readiness.database_status(s) for _ in range(3)] == ["unreachable"] * 3 and len(calls) == 1


def test_health_endpoint(env):
    client = TestClient(main.app)
    env.setattr("httpx.get", responder())
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and (r.json()["status"], r.json()["database"]) == ("ok", "ok")
    readiness._cache.clear()
    env.setattr("httpx.get", responder(rest=401))
    r = client.get("/api/v1/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded"
    assert "sb_secret" not in r.text and "ref.supabase" not in r.text


@pytest.mark.parametrize("provider,key,expected", [
    ("gemini", "", "no_api_key"), ("nope", "k", "unknown_provider"), ("huggingface", "k", "not_installed"),
])
def test_embedding_problems(env, provider, key, expected):
    env.setattr("app.core.config.find_spec", lambda _name: None)
    env.setenv("EMBEDDING_PROVIDER", provider)
    env.setenv("GEMINI_API_KEY", key)
    assert readiness.embedding_status(get_settings()) == expected
