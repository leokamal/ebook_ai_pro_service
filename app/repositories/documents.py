from typing import Optional

from app.core.exceptions import Conflict, NotFound
from app.repositories.base import paginate

DOC_COLUMNS = ("id,user_id,title,file_name,file_path,file_type,file_size,page_count,language,status,"
               "index_state,embedding_model,chunk_count,error_message,created_at,updated_at")


def build_file_path(user_id: str, document_id: str, file_name: str) -> str:
    """Storage path is always derived server-side: {user_id}/{document_id}/{file_name}."""
    if (not file_name or "/" in file_name or "\\" in file_name or file_name in (".", "..")
            or any(ord(c) < 32 for c in file_name)):
        from app.core.exceptions import AppError
        raise AppError("validation_error", "Invalid file name", 422)
    return f"{user_id}/{document_id}/{file_name}"


class DocumentRepository:
    """`db` is a user-scoped client (RLS applies). Rows of other users simply do not exist for it."""

    def __init__(self, db):
        self.db = db

    def get(self, document_id: str) -> dict:
        rows = self.db.table("documents").select(DOC_COLUMNS).eq("id", document_id).limit(1).execute().data
        if not rows:
            raise NotFound("Document not found")
        return rows[0]

    def list(self, limit=None, cursor=None, status: Optional[str] = None) -> dict:
        q = self.db.table("documents").select(DOC_COLUMNS)
        if status:
            q = q.eq("status", status)
        q, page = paginate(q, limit, cursor)
        return page(q.execute().data)

    def list_searchable(self, max_documents: int = 500) -> list[dict]:
        """The caller's completed + indexed documents (RLS-scoped), deterministic order."""
        return (self.db.table("documents").select("id,title,language").eq("status", "completed")
                .eq("index_state", "indexed").order("created_at", desc=True).order("id", desc=True)
                .limit(max_documents).execute().data)

    def count(self, cap: int = 1000) -> int:
        """Number of the caller's documents (RLS-scoped), capped for cheapness."""
        return len(self.db.table("documents").select("id").limit(cap).execute().data)

    def create(self, user_id: str, document_id: str, title: str, file_name: str, file_size: int,
               language: Optional[str] = None) -> tuple[dict, bool]:
        """Returns (row, created). Same id + same owner -> existing row. user_id comes from the verified token."""
        path = build_file_path(user_id, document_id, file_name)
        existing = self.db.table("documents").select(DOC_COLUMNS).eq("id", document_id).limit(1).execute().data
        if existing:
            return existing[0], False
        row = {"id": document_id, "user_id": user_id, "title": title, "file_name": file_name,
               "file_path": path, "file_size": file_size, "language": language}
        try:
            return self.db.table("documents").insert(row).execute().data[0], True
        except Exception as e:  # PK collision with another user's id is invisible via RLS
            if "23505" in str(e) or "duplicate" in str(e).lower():
                raise Conflict("Document id already in use") from None
            raise

    def update_title(self, document_id: str, title: str) -> dict:
        rows = self.db.table("documents").update({"title": title}).eq("id", document_id).execute().data
        if not rows:
            raise NotFound("Document not found")
        return rows[0]


class DocumentAdminRepository:
    """Service-role access for the worker/backend. EVERY method needs user_id and filters on it,
    because the service role bypasses RLS."""

    def __init__(self, service_db):
        self.db = service_db

    def get_owned(self, document_id: str, user_id: str) -> dict:
        rows = (self.db.table("documents").select(DOC_COLUMNS).eq("id", document_id)
                .eq("user_id", user_id).limit(1).execute().data)
        if not rows:
            raise NotFound("Document not found")
        return rows[0]

    MUTABLE = {"status", "index_state", "embedding_model", "chunk_count", "page_count", "error_message", "language"}

    def update_owned(self, document_id: str, user_id: str, fields: dict, only_if_status: Optional[str] = None) -> dict:
        """`only_if_status` makes the write conditional (compare-and-set): a concurrent DELETE that moved the
        row to `deleting` is never overwritten; 0 rows then raises NotFound."""
        if not set(fields) <= self.MUTABLE:
            raise ValueError("Unsupported document fields")
        q = self.db.table("documents").update(fields).eq("id", document_id).eq("user_id", user_id)
        if only_if_status:
            q = q.eq("status", only_if_status)
        rows = q.execute().data
        if not rows:
            raise NotFound("Document not found")
        return rows[0]

    def delete_owned(self, document_id: str, user_id: str) -> None:
        self.db.table("documents").delete().eq("id", document_id).eq("user_id", user_id).execute()

    # ---- deliberately CROSS-USER (maintenance only: janitor, reconcile, reindex). Never call these from a
    # request handler on behalf of a user; each row carries its own user_id, which callers must use.
    def list_deleting(self, limit: int = 20) -> list[dict]:
        return (self.db.table("documents").select("id,user_id,file_path").eq("status", "deleting")
                .order("updated_at").order("id").limit(limit).execute().data)

    def list_indexed(self, limit: int, offset: int) -> list[dict]:
        return (self.db.table("documents").select("id,user_id,chunk_count,status,index_state")
                .eq("status", "completed").order("id").range(offset, offset + limit - 1).execute().data)


class ChunkRepository:
    """Service-role writer for `document_chunks` (ids/pages only: no text, no vectors)."""

    def __init__(self, service_db):
        self.db = service_db

    def replace(self, document_id: str, user_id: str, rows: list[dict], batch: int = 500) -> None:
        self.db.table("document_chunks").delete().eq("document_id", document_id).eq("user_id", user_id).execute()
        for i in range(0, len(rows), batch):
            self.db.table("document_chunks").insert([
                {"id": r["id"], "document_id": document_id, "user_id": user_id,
                 "chunk_index": r["chunk_index"], "page_number": r["page_number"]} for r in rows[i:i + batch]
            ]).execute()

    def count(self, document_id: str, user_id: str) -> int:
        return len(self.db.table("document_chunks").select("id").eq("document_id", document_id)
                   .eq("user_id", user_id).execute().data)
