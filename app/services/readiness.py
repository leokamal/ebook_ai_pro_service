"""Can this deployment actually work? Used by `/api/v1/health`. Never loads a model or opens Chroma.

Results are short codes only: the endpoint is public, so no URLs, keys or error text.
"""
import logging
import threading
import time

from app.core.config import Settings, embedding_problem

logger = logging.getLogger("uvicorn.error")
CACHE_SECONDS = 30  # the endpoint is unauthenticated: do not let it hammer Supabase
_lock = threading.Lock()
_cache: dict = {}


def _probe(s: Settings, get) -> str:
    """ok | unreachable | bad_anon_key | bad_service_key | schema_outdated | error"""
    base = s.supabase_url
    try:
        if get(f"{base}/auth/v1/settings", headers={"apikey": s.anon_key}, timeout=5).status_code in (401, 403):
            return "bad_anon_key"
        # `error_code` is the newest column the worker writes: a 400 means a migration was not applied
        r = get(f"{base}/rest/v1/documents?select=id,error_code&limit=1", timeout=5,
                headers={"apikey": s.service_role_key, "Authorization": f"Bearer {s.service_role_key}"})
    except Exception as e:  # DNS, TLS, timeout, malformed URL
        logger.warning("readiness: cannot reach Supabase (%s)", type(e).__name__)
        return "unreachable"
    return {200: "ok", 400: "schema_outdated", 404: "schema_outdated", 401: "bad_service_key",
            403: "bad_service_key"}.get(r.status_code, "error")


def database_status(s: Settings) -> str:
    if not (s.configured and s.service_role_key):
        return "unconfigured"
    import httpx
    key = (s.supabase_url, s.anon_key, s.service_role_key)
    with _lock:
        at, status = _cache.get(key, (0.0, ""))
        if not status or time.monotonic() - at >= CACHE_SECONDS:
            status = _probe(s, httpx.get)
            _cache[key] = (time.monotonic(), status)
        return status


def embedding_status(s: Settings) -> str:
    problem = embedding_problem(s)
    return problem[0] if problem else "ok"
