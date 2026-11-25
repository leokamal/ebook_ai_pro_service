from fastapi import APIRouter, Response

from app.core.dependencies import SettingsDep, get_embedding_service, peek_vector_store
from app.schemas.system import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Pro API health (no auth)")
def health(response: Response, s: SettingsDep) -> HealthResponse:
    """Cheap: never loads the embedding model and never opens Chroma if nothing else did."""
    ok = s.configured and bool(s.service_role_key)
    store = peek_vector_store()
    index = store.health() if store is not None and store.opened else {"state": "not_initialized"}
    if index.get("state") in ("needs_reindex", "error") or not ok:
        response.status_code = 503
    from app.workers.manager import get_runner
    runner = get_runner()
    return HealthResponse(worker="running" if runner and runner.alive else "stopped", status="ok" if ok else "unconfigured", environment=s.environment,
                          embedding_model=s.embedding_model,
                          embedding_loaded=get_embedding_service().loaded, vector_index=index)
