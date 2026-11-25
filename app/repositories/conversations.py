from datetime import datetime, timezone
from typing import Optional

from app.core.exceptions import AppError, NotFound
from app.repositories.base import paginate

CONV_COLUMNS = "id,user_id,title,mode,document_id,created_at,updated_at"
MSG_COLUMNS = "id,conversation_id,role,content,token_count,created_at"
SRC_COLUMNS = "id,message_id,document_id,chunk_id,page_number,relevance_score,document_title"


class ConversationRepository:
    """User-scoped client: RLS guarantees ownership; foreign ids behave as missing (404)."""

    def __init__(self, db):
        self.db = db

    def create(self, user_id: str, mode: str, document_id: Optional[str], title: Optional[str]) -> dict:
        if (mode == "single_document") != bool(document_id):
            raise AppError("validation_error", "document_id is required for single_document and forbidden "
                           "for all_documents", 422)
        if document_id:  # RLS hides other users' documents, so this doubles as the ownership check
            if not self.db.table("documents").select("id").eq("id", document_id).limit(1).execute().data:
                raise NotFound("Document not found")
        row = {"user_id": user_id, "mode": mode, "document_id": document_id}
        if title:
            row["title"] = title
        return self.db.table("conversations").insert(row).execute().data[0]

    def get(self, conversation_id: str) -> dict:
        rows = self.db.table("conversations").select(CONV_COLUMNS).eq("id", conversation_id).limit(1).execute().data
        if not rows:
            raise NotFound("Conversation not found")
        return rows[0]

    def list(self, limit=None, cursor=None) -> dict:
        q, page = paginate(self.db.table("conversations").select(CONV_COLUMNS), limit, cursor, key="updated_at")
        return page(q.execute().data)

    def update_title(self, conversation_id: str, title: str) -> dict:
        rows = self.db.table("conversations").update({"title": title}).eq("id", conversation_id).execute().data
        if not rows:
            raise NotFound("Conversation not found")
        return rows[0]

    def delete(self, conversation_id: str) -> None:
        self.get(conversation_id)
        self.db.table("conversations").delete().eq("id", conversation_id).execute()

    def recent_messages(self, conversation_id: str, n: int) -> list[dict]:
        """Last n messages, oldest first (for the LLM history). Ownership is enforced by RLS."""
        rows = (self.db.table("messages").select("id,role,content,created_at").eq("conversation_id", conversation_id)
                .order("created_at", desc=True).order("id", desc=True).limit(n).execute().data)
        return list(reversed(rows))

    def list_messages(self, conversation_id: str, limit=None, cursor=None) -> dict:
        self.get(conversation_id)  # 404 for foreign/missing conversations
        q, page = paginate(self.db.table("messages").select(MSG_COLUMNS).eq("conversation_id", conversation_id),
                           limit, cursor)
        result = page(q.execute().data)
        ids = [m["id"] for m in result["items"]]
        by_msg: dict = {i: [] for i in ids}
        if ids:
            for s in self.db.table("message_sources").select(SRC_COLUMNS).in_("message_id", ids).execute().data:
                by_msg[s["message_id"]].append(s)
        for m in result["items"]:
            m["sources"] = by_msg[m["id"]]
        return result


class MessageWriter:
    """Service-role writer: clients cannot insert messages. Always verifies conversation ownership first."""

    def __init__(self, service_db):
        self.db = service_db

    def _assert_owned(self, conversation_id: str, user_id: str) -> None:
        rows = (self.db.table("conversations").select("id").eq("id", conversation_id)
                .eq("user_id", user_id).limit(1).execute().data)
        if not rows:
            raise NotFound("Conversation not found")

    def add_message(self, conversation_id: str, user_id: str, role: str, content: str, token_count: int = 0) -> dict:
        self._assert_owned(conversation_id, user_id)
        row = {"conversation_id": conversation_id, "role": role, "content": content, "token_count": token_count}
        msg = self.db.table("messages").insert(row).execute().data[0]
        self.db.table("conversations").update({"updated_at": datetime.now(timezone.utc).isoformat()}) \
            .eq("id", conversation_id).execute()
        return msg

    def set_default_title(self, conversation_id: str, user_id: str, title: str, default: str) -> None:
        """Auto-title: only while the title is still the default; ownership is part of the filter."""
        (self.db.table("conversations").update({"title": title}).eq("id", conversation_id)
         .eq("user_id", user_id).eq("title", default).execute())

    def delete_message(self, message_id: str, conversation_id: str, user_id: str) -> None:
        self._assert_owned(conversation_id, user_id)
        self.db.table("messages").delete().eq("id", message_id).eq("conversation_id", conversation_id).execute()

    def add_sources(self, message_id: str, conversation_id: str, user_id: str, sources: list[dict]) -> None:
        self._assert_owned(conversation_id, user_id)
        if not self.db.table("messages").select("id").eq("id", message_id).eq(
                "conversation_id", conversation_id).limit(1).execute().data:
            raise NotFound("Message not found")
        allowed = ("document_id", "chunk_id", "page_number", "relevance_score", "document_title")
        rows = [{**{k: s.get(k) for k in allowed}, "message_id": message_id} for s in sources]
        for doc_id in {r["document_id"] for r in rows if r["document_id"]}:  # never cite another user's document
            if not self.db.table("documents").select("id").eq("id", doc_id).eq("user_id", user_id).limit(1).execute().data:
                raise NotFound("Document not found")
        if rows:
            self.db.table("message_sources").insert(rows).execute()
