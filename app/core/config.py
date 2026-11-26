"""Pro settings, read from the environment on each call (cheap; keeps tests simple).

Never hardcode secrets.
"""
import os
from dataclasses import dataclass
from typing import Optional


def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    anon_key: str
    service_role_key: str
    jwks_url: str
    jwt_secret: Optional[str]
    storage_bucket: str
    environment: str
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dimension: int = 384
    embed_batch: int = 32
    chroma_dir: str = "./chroma_data"
    chroma_collection: str = "ebook_ai_pro_documents"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    llm_timeout: float = 60.0
    llm_max_retries: int = 2
    llm_max_output_tokens: int = 1024
    llm_stream_deadline: float = 120.0
    history_messages: int = 6
    history_max_tokens: int = 1500
    rag_candidates: int = 20
    rag_context_chunks: int = 6
    rag_min_score: Optional[float] = None
    rag_max_per_doc: int = 3
    rag_condense: bool = True
    allowed_providers: tuple = ("google", "facebook")  # empty = any provider (local dev only)
    orphan_max_age_hours: int = 24

    @property
    def issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def configured(self) -> bool:
        return bool(self.supabase_url and self.anon_key)


def _providers() -> tuple:
    raw = os.environ.get("AUTH_ALLOWED_PROVIDERS")  # unset -> default; set-but-empty -> any provider
    if raw is None:
        return Settings.allowed_providers
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def get_settings() -> Settings:
    url = (_env("SUPABASE_URL", default="") or "").rstrip("/")
    return Settings(
        supabase_url=url,
        anon_key=_env("SUPABASE_ANON_KEY", default=""),
        service_role_key=_env("SUPABASE_SERVICE_ROLE_KEY", default=""),
        jwks_url=_env("SUPABASE_JWT_JWKS_URL", default=f"{url}/auth/v1/.well-known/jwks.json" if url else ""),
        jwt_secret=_env("SUPABASE_JWT_SECRET"),
        storage_bucket=_env("SUPABASE_STORAGE_BUCKET", default="user-documents"),
        environment=_env("ENVIRONMENT", default="development"),
        embedding_model=_env("EMBEDDING_MODEL", default=Settings.embedding_model),
        embedding_dimension=int(_env("EMBEDDING_DIMENSION", default=Settings.embedding_dimension)),
        embed_batch=int(_env("EMBED_BATCH", default=Settings.embed_batch)),
        chroma_dir=_env("CHROMA_PERSIST_DIRECTORY", default=Settings.chroma_dir),
        chroma_collection=_env("CHROMA_COLLECTION_NAME", default=Settings.chroma_collection),
        gemini_api_key=_env("GEMINI_API_KEY", default=""),
        gemini_model=_env("GEMINI_MODEL", default=Settings.gemini_model),
        llm_timeout=float(_env("LLM_TIMEOUT_SECONDS", default=Settings.llm_timeout)),
        llm_max_retries=int(_env("LLM_MAX_RETRIES", default=Settings.llm_max_retries)),
        llm_max_output_tokens=int(_env("LLM_MAX_OUTPUT_TOKENS", default=Settings.llm_max_output_tokens)),
        llm_stream_deadline=float(_env("LLM_STREAM_DEADLINE_SECONDS", default=Settings.llm_stream_deadline)),
        history_messages=int(_env("HISTORY_MESSAGES", default=Settings.history_messages)),
        history_max_tokens=int(_env("HISTORY_MAX_TOKENS", default=Settings.history_max_tokens)),
        rag_candidates=int(_env("RAG_CANDIDATES", default=Settings.rag_candidates)),
        rag_context_chunks=int(_env("RAG_CONTEXT_CHUNKS", default=Settings.rag_context_chunks)),
        rag_min_score=float(_env("RAG_MIN_SCORE")) if _env("RAG_MIN_SCORE") else None,
        rag_max_per_doc=int(_env("RAG_MAX_PER_DOC", default=Settings.rag_max_per_doc)),
        rag_condense=_env("RAG_CONDENSE_QUESTION", default="true").lower() not in ("0", "false", "no"),
        allowed_providers=_providers(),
        orphan_max_age_hours=int(_env("ORPHAN_MAX_AGE_HOURS", default=Settings.orphan_max_age_hours)),
    )
