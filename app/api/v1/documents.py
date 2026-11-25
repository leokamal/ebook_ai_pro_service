"""Document endpoints. Thin handlers: validation + ownership here, work in services/repositories.

All routes are plain `def` (FastAPI runs them in the threadpool), because the Supabase/Chroma clients
are blocking. Ownership is established through the user-scoped repository (RLS) BEFORE any
service-role call touches the document.
"""
import logging
import os
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response

from app.core.dependencies import (CurrentUserDep, SettingsDep, build_ingestion, get_admin_docs, get_documents_repo,
                                   get_job_queue, get_job_reader, get_storage)
from app.core.exceptions import AppError, Conflict, NotFound, ServiceUnavailable
from app.repositories.documents import (DocumentAdminRepository, DocumentRepository, build_file_path)
from app.repositories.jobs import JobQueue, JobReader
from app.schemas.documents import (DocumentCreate, DocumentOut, DocumentPage, DocumentStatusOut, JobAccepted)

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/documents", tags=["documents"])

DocsDep = Annotated[DocumentRepository, Depends(get_documents_repo)]
AdminDocsDep = Annotated[DocumentAdminRepository, Depends(get_admin_docs)]
QueueDep = Annotated[JobQueue, Depends(get_job_queue)]
JobsDep = Annotated[JobReader, Depends(get_job_reader)]
_ERR = {401: {"description": "Missing/invalid token"}, 404: {"description": "Not found (or not yours)"}}


def _limit(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _check_quota(docs: DocumentRepository, jobs: JobReader, new_document: bool):
    cap = _limit("MAX_DOCS_PER_USER", 200)
    if new_document and docs.count(cap + 1) >= cap:
        raise AppError("quota_exceeded", "Document limit reached", 429)
    if jobs.active_count() >= _limit("MAX_ACTIVE_JOBS_PER_USER", 5):
        raise AppError("quota_exceeded", "Too many documents are being processed; try again shortly", 429)


def _max_pdf_bytes() -> int:
    return int(os.environ.get("MAX_PDF_BYTES", 52428800))


@router.post("", response_model=DocumentOut, status_code=201, responses={**_ERR, 200: {"model": DocumentOut},
             409: {"description": "Id already used"}, 422: {"description": "File missing/invalid"}})
def register_document(body: DocumentCreate, response: Response, user: CurrentUserDep, s: SettingsDep, docs: DocsDep,
                      admin: AdminDocsDep, queue: QueueDep, jobs: JobsDep, storage=Depends(get_storage)):
    """Register a PDF that Flutter already uploaded to `{user_id}/{document_id}/{file_name}`."""
    doc_id = str(body.document_id)
    path = build_file_path(user.id, doc_id, body.file_name)  # derived server-side, validated
    try:
        info = storage.inspect_object(s.storage_bucket, path)  # service role, but only on the caller's own path
    except Exception:
        logger.exception("storage inspect failed")
        raise ServiceUnavailable("Storage unavailable", code="storage_unavailable") from None
    if info is None:
        raise AppError("file_not_found", "Upload the PDF to storage first", 422)
    size, head = info
    if size <= 0:
        raise AppError("invalid_file", "Could not determine the file size", 422)
    if size > _max_pdf_bytes():
        raise AppError("payload_too_large", "The PDF exceeds the allowed size", 413)
    if not head.startswith(b"%PDF-"):
        raise AppError("invalid_file", "The file is not a PDF", 422)

    try:
        docs.get(doc_id)
        exists = True
    except NotFound:
        exists = False
    if not exists:
        _check_quota(docs, jobs, True)
    doc, created = docs.create(user.id, doc_id, body.title, body.file_name, size, body.language)
    if created and body.auto_process:
        queue.enqueue(doc_id, user.id)
        doc = admin.get_owned(doc_id, user.id)
    response.status_code = 201 if created else 200
    return doc


@router.get("", response_model=DocumentPage)
def list_documents(user: CurrentUserDep, docs: DocsDep, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                   cursor: Optional[str] = None, status: Optional[str] = None):
    return docs.list(limit, cursor, status)


@router.get("/{document_id}", response_model=DocumentOut, responses=_ERR)
def get_document(document_id: str, user: CurrentUserDep, docs: DocsDep):
    return docs.get(_uuid(document_id))


@router.delete("/{document_id}", status_code=202, response_model=JobAccepted, responses=_ERR)
def delete_document(document_id: str, background: BackgroundTasks, user: CurrentUserDep, docs: DocsDep,
                    admin: AdminDocsDep):
    document_id = _uuid(document_id)
    docs.get(document_id)  # ownership (404 otherwise)
    row = admin.update_owned(document_id, user.id, {"status": "deleting"})
    # Quick path; the worker's janitor finishes it if this process dies first.
    background.add_task(_finish_delete, {"id": document_id, "user_id": user.id, "file_path": row["file_path"]})
    return {"document_id": document_id, "status": "deleting"}


def _finish_delete(doc: dict):
    try:
        build_ingestion().delete_document(doc)
    except Exception:
        logger.exception("inline delete failed; janitor will retry")


@router.post("/{document_id}/process", status_code=202, response_model=JobAccepted,
             responses={**_ERR, 409: {"description": "Not in `uploaded` state"}})
def process_document(document_id: str, user: CurrentUserDep, docs: DocsDep, queue: QueueDep, jobs: JobsDep):
    document_id = _uuid(document_id)
    if docs.get(document_id)["status"] != "uploaded":
        raise Conflict("Only documents in 'uploaded' state can be processed")
    _check_quota(docs, jobs, False)
    return {"document_id": document_id, "status": "queued", "job_id": queue.enqueue(document_id, user.id)}


@router.post("/{document_id}/retry", status_code=202, response_model=JobAccepted,
             responses={**_ERR, 409: {"description": "Not in `failed` state"}})
def retry_document(document_id: str, user: CurrentUserDep, docs: DocsDep, queue: QueueDep, jobs: JobsDep):
    document_id = _uuid(document_id)
    if docs.get(document_id)["status"] != "failed":
        raise Conflict("Only failed documents can be retried")
    _check_quota(docs, jobs, False)
    return {"document_id": document_id, "status": "queued", "job_id": queue.enqueue(document_id, user.id)}


@router.get("/{document_id}/status", response_model=DocumentStatusOut, responses=_ERR)
def document_status(document_id: str, user: CurrentUserDep, docs: DocsDep, jobs: JobsDep):
    doc = docs.get(_uuid(document_id))
    job = jobs.latest_for_document(doc["id"]) or {}
    return {"status": doc["status"], "index_state": doc["index_state"], "progress": job.get("progress", 0),
            "attempts": job.get("attempts", 0), "error_message": doc.get("error_message"),
            "page_count": doc.get("page_count"), "chunk_count": doc.get("chunk_count", 0)}


def _uuid(value: str) -> str:
    import uuid
    try:
        return str(uuid.UUID(value))
    except ValueError:
        from app.core.exceptions import NotFound
        raise NotFound("Document not found") from None
