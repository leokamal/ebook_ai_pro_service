# Workaround to ensure `pysqlite3` is used as `sqlite3` (must run before chromadb is imported)
import sys
try:
    __import__('pysqlite3')
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()  # load SUPABASE_* / GEMINI_API_KEY ... from a local .env (git-ignored)
except ImportError:
    pass

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import router as api_v1_router  # light imports only: heavy libraries load lazily
from app.core.exceptions import install_error_handlers


@asynccontextmanager
async def lifespan(_app):
    from app.workers.manager import start_worker, stop_worker
    start_worker()  # job runner: no-op when the Supabase/Gemini environment is not configured
    yield
    stop_worker()
    from app.core.dependencies import shutdown_infrastructure
    shutdown_infrastructure()  # close the Chroma client if it was opened


app = FastAPI(
    lifespan=lifespan,
    title="E-Book AI Pro",
    version="3.0.0",
    description=("Per-user PDF library with Personal RAG / Multi-RAG chat, page-level citations and semantic search "
                 "(Arabic, French, English). Authenticate every call with the Supabase access token "
                 "(`Authorization: Bearer`). See docs/API.md."),
    openapi_tags=[{"name": "documents", "description": "Register, process and manage your PDFs"},
                  {"name": "conversations", "description": "Chat sessions and their history"},
                  {"name": "chat", "description": "Grounded Q&A with citations (JSON or SSE stream)"},
                  {"name": "search", "description": "Semantic search without an LLM"},
                  {"name": "system", "description": "Health"},
                  {"name": "admin", "description": "Operator endpoints (X-Admin-Token)"}])

install_error_handlers(app)  # request ids + the /api/v1 error envelope
app.include_router(api_v1_router, prefix="/api/v1")

# Auth is a Bearer header (no cookies), so credentials are off. CORS_ORIGINS="https://app.example,https://x.example"
# only matters for browser clients (Flutter web); native apps are unaffected.
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-ID", "Retry-After"])


@app.get("/health", include_in_schema=False)
def liveness():
    """Cheap liveness probe for the platform (no database, no models). Details: /api/v1/health."""
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
