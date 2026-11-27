"""PostgREST access. Two kinds of client, never mixed up:

* user_db(token)  - anon key + the caller's verified JWT -> RLS is enforced by Postgres.
* service_db()    - service-role key -> BYPASSES RLS. Only repository methods that take an explicit
                    user_id and filter on it may use it (see `Owned*` methods).
"""
import base64
import uuid
from datetime import datetime
from typing import Optional

from app.core.config import get_settings
from app.core.exceptions import AppError, ServiceUnavailable

DEFAULT_LIMIT, MAX_LIMIT = 20, 100


def _make(headers):
    from postgrest import SyncPostgrestClient  # lazy; comes with the supabase package
    return SyncPostgrestClient(f"{get_settings().supabase_url}/rest/v1", headers=headers, timeout=15)


def user_db(access_token: str):
    s = get_settings()
    if not s.configured:
        raise ServiceUnavailable()
    return _make({"apikey": s.anon_key, "Authorization": f"Bearer {access_token}"})


def service_db():
    s = get_settings()
    if not (s.supabase_url and s.service_role_key):
        raise ServiceUnavailable()
    return _make({"apikey": s.service_role_key, "Authorization": f"Bearer {s.service_role_key}"})


def fetch_all(make_query, page: int = 500) -> list:
    """Every row of a query: PostgREST caps one response (max rows), whatever `limit` asks for.
    `make_query` builds a fresh, ordered query each time."""
    out, offset = [], 0
    while True:
        rows = make_query().range(offset, offset + page - 1).execute().data
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


# ---- keyset pagination on (created_at desc, id desc); cursor is opaque to clients
def encode_cursor(row: dict, key: str = "created_at") -> str:
    return base64.urlsafe_b64encode(f"{row[key]}|{row['id']}".encode()).decode()


def decode_cursor(cursor: str):
    """Returns (timestamp, uuid) as validated strings; anything malformed is a 400 (never reaches a filter)."""
    try:
        ts, _, rid = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        if any(c in ts for c in ",()"):  # PostgREST filter delimiters
            raise ValueError
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return ts, str(uuid.UUID(rid))
    except Exception:
        raise AppError("validation_error", "Invalid cursor", 400) from None


def clamp_limit(limit: Optional[int]) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))


def paginate(query, limit, cursor, key="created_at"):
    """Apply order + cursor + limit+1 to a select query; returns a function mapping rows -> page."""
    limit = clamp_limit(limit)
    if cursor:
        ts, rid = decode_cursor(cursor)
        query = query.or_(f"{key}.lt.{ts},and({key}.eq.{ts},id.lt.{rid})")
    query = query.order(key, desc=True).order("id", desc=True).limit(limit + 1)

    def page(rows):
        items = rows[:limit]
        nxt = encode_cursor(items[-1], key) if len(rows) > limit else None
        return {"items": items, "next_cursor": nxt}
    return query, page
