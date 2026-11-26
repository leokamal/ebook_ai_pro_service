"""Service-role access for the account screen, account deletion and orphan cleanup.

The service role bypasses RLS: every per-user method takes the VERIFIED user id and filters on it.
`list_orphan_objects` / `path_registered` are deliberately cross-user (maintenance only).
"""
DOCUMENTS = "documents"


def _names(data) -> list:
    """PostgREST returns `setof text` as a list of strings; tolerate a list of single-key objects."""
    return [d if isinstance(d, str) else next(iter(d.values())) for d in (data or [])]


class AccountRepository:
    def __init__(self, service_db):
        self.db = service_db

    def profile(self, user_id: str) -> dict:
        rows = (self.db.table("profiles").select("generated_key,email,full_name,display_name,avatar_url,avatar_hidden,country,gender")
                .eq("id", user_id).limit(1).execute().data)
        return rows[0] if rows else {}

    def usage(self, user_id: str) -> dict:
        data = self.db.rpc("user_storage_usage", {"p_user": user_id}).execute().data
        row = (data[0] if isinstance(data, list) and data else data) or {}
        return {"bytes": int(row.get("bytes", 0)), "objects": int(row.get("objects", 0))}

    def limits(self) -> dict:
        rows = self.db.table("app_limits").select("max_storage_bytes,max_objects").limit(1).execute().data
        return rows[0] if rows else {"max_storage_bytes": 0, "max_objects": 0}  # fail closed, like the policy

    def document_count(self, user_id: str) -> int:
        return len(self.db.table(DOCUMENTS).select("id").eq("user_id", user_id).limit(10000).execute().data)

    def mark_all_deleting(self, user_id: str) -> None:
        self.db.table(DOCUMENTS).update({"status": "deleting"}).eq("user_id", user_id).execute()

    def list_user_objects(self, user_id: str) -> list:
        return _names(self.db.rpc("list_user_objects", {"p_user": user_id}).execute().data)

    def list_orphan_objects(self, older_than_hours: int, limit: int = 50) -> list:
        return _names(self.db.rpc("list_orphan_objects", {"p_older_than": f"{older_than_hours} hours",
                                                          "p_limit": limit}).execute().data)

    def path_registered(self, path: str) -> bool:
        return bool(self.db.table(DOCUMENTS).select("id").eq("file_path", path).limit(1).execute().data)
