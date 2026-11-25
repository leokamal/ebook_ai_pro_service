"""Shared FastAPI dependencies + Annotated aliases (use the aliases in route signatures)."""
import os
from functools import lru_cache
from typing import Annotated, Optional

from fastapi import Depends, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.exceptions import ServiceUnavailable, Unauthorized
from app.core.security import AuthUser, verify_token


_bearer = HTTPBearer(auto_error=False, description="Supabase access token (the signed-in user's JWT)")


def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer)) -> AuthUser:
    """The user id comes only from the verified token, never from the request body or query."""
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials.strip():
        raise Unauthorized()
    return verify_token(creds.credentials.strip())


# ---- infrastructure singletons (lazy; creating them loads no model and opens no database)
@lru_cache(maxsize=1)
def _embedder(model: str, dimension: int, batch: int):
    from app.services.embedding import EmbeddingService
    return EmbeddingService(model, dimension, batch)


def get_embedding_service():
    s = get_settings()
    return _embedder(s.embedding_model, s.embedding_dimension, s.embed_batch)


_created: dict = {}


@lru_cache(maxsize=1)
def _store(path: str, collection: str, model: str, dimension: int, batch: int):
    from app.services.vector_store import ChromaVectorStore
    _created["store"] = ChromaVectorStore(path, collection, model, dimension, batch)
    return _created["store"]


def get_vector_store():
    s = get_settings()
    return _store(s.chroma_dir, s.chroma_collection, s.embedding_model, s.embedding_dimension, s.embed_batch)


def peek_vector_store():
    """The store if one was already created (whatever the settings), else None. Never creates one."""
    return _created.get("store")


def shutdown_infrastructure():
    store = peek_vector_store()
    if store is not None:
        store.close()


# ---- Supabase access (user-scoped = RLS; service = bypasses RLS, used only with explicit ownership checks)
def get_user_db(user: AuthUser = Depends(get_current_user)):
    from app.repositories.base import user_db
    db = user_db(user.access_token)
    try:
        yield db
    finally:
        try:
            db.session.close()
        except Exception:
            pass


@lru_cache(maxsize=1)
def _service_db():
    from app.repositories.base import service_db
    return service_db()


def get_service_db():
    return _service_db()


def get_storage():
    import supabase_storage  # legacy module, extended with Pro helpers
    return supabase_storage


def get_job_queue(db=Depends(get_service_db)):
    from app.repositories.jobs import JobQueue
    return JobQueue(db)


def get_admin_docs(db=Depends(get_service_db)):
    from app.repositories.documents import DocumentAdminRepository
    return DocumentAdminRepository(db)


def get_documents_repo(db=Depends(get_user_db)):
    from app.repositories.documents import DocumentRepository
    return DocumentRepository(db)


def get_job_reader(db=Depends(get_user_db)):
    from app.repositories.jobs import JobReader
    return JobReader(db)


def build_ingestion(settings=None):
    """IngestionService for the worker / maintenance (no request context)."""
    import os
    from app.repositories.documents import ChunkRepository, DocumentAdminRepository
    from app.services.ingestion import IngestConfig, IngestionService
    s = settings or get_settings()
    db = get_service_db()
    cfg = IngestConfig(bucket=s.storage_bucket, max_pdf_bytes=int(os.environ.get("MAX_PDF_BYTES", 52428800)),
                       max_pdf_pages=int(os.environ.get("MAX_PDF_PAGES", 1000)), embed_batch=s.embed_batch)
    return IngestionService(DocumentAdminRepository(db), ChunkRepository(db), get_storage(),
                            get_embedding_service(), get_vector_store(), cfg)


def get_maintenance(db=Depends(get_service_db)):
    from app.repositories.documents import DocumentAdminRepository
    from app.repositories.jobs import JobQueue
    from app.services.maintenance import Maintenance
    return Maintenance(DocumentAdminRepository(db), JobQueue(db), get_vector_store(), build_ingestion())


def get_conversations_repo(db=Depends(get_user_db)):
    from app.repositories.conversations import ConversationRepository
    return ConversationRepository(db)


@lru_cache(maxsize=1)
def _llm(api_key: str, model: str, timeout: float, retries: int, max_tokens: int):
    from app.services.llm import GeminiService
    return GeminiService(api_key, model, timeout, retries, max_tokens)


def get_llm():
    s = get_settings()
    return _llm(s.gemini_api_key, s.gemini_model, s.llm_timeout, s.llm_max_retries, s.llm_max_output_tokens)


def get_orchestrator(user: AuthUser = Depends(get_current_user), db=Depends(get_user_db),
                     svc=Depends(get_service_db), llm=Depends(get_llm)):
    """Per-request orchestrator: user-scoped repos (RLS) + service-role writer that re-checks ownership."""
    from app.repositories.conversations import ConversationRepository, MessageWriter
    from app.repositories.documents import DocumentRepository
    from app.services.orchestrator import RAGOrchestrator
    from app.services.rag_parts import CitationService, ContextBuilder, ConversationMemoryService, RerankingService
    from app.services.retrieval import RetrievalService
    s = get_settings()
    return RAGOrchestrator(
        user.id, ConversationRepository(db), DocumentRepository(db),
        RetrievalService(get_embedding_service(), get_vector_store()),
        ConversationMemoryService(s.history_messages, s.history_max_tokens),
        RerankingService(s.rag_min_score, s.rag_max_per_doc), ContextBuilder(), CitationService(), llm,
        MessageWriter(svc), s)


SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[AuthUser, Depends(get_current_user)]
