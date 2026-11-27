from fastapi import APIRouter, Response

from app.core.dependencies import SettingsDep, get_embedding_service, peek_vector_store
from app.schemas.system import HealthResponse
from app.services.readiness import database_status, embedding_status

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Pro API health (no auth)")
def health(response: Response, s: SettingsDep) -> HealthResponse:
    """Readiness: 503 unless the database and embedding provider can work. Never loads the embedding model and
    never opens Chroma if nothing else did. Platform liveness checks use `/health` instead."""
    database, embedding = database_status(s), embedding_status(s)
    if database == "unconfigured":
        status = "unconfigured"
    else:
        status = "ok" if database == "ok" and embedding == "ok" else "degraded"
    store = peek_vector_store()
    index = store.health() if store is not None and store.opened else {"state": "not_initialized"}
    if status != "ok" or index.get("state") in ("needs_reindex", "error"):
        response.status_code = 503
    from app.workers.manager import get_runner
    runner = get_runner()
    return HealthResponse(
        status=status, database=database, embedding=embedding, environment=s.environment,
        embedding_provider=s.embedding_provider, embedding_model=s.embedding_model,
        embedding_loaded=embedding == "ok" and get_embedding_service().loaded, vector_index=index,
        worker="running" if runner and runner.alive else "stopped")
