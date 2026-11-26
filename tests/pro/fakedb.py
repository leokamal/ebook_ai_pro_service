"""Tiny in-memory stand-in for the PostgREST client: enough for the repositories.

`FakeDB(rows, user_id=...)` mimics RLS for a user-scoped client by hiding rows whose user_id differs
(conversations/documents) - the service client passes user_id=None and sees everything.
"""
import copy
import uuid


class _Result:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class _Query:
    DEFAULTS = {"documents": {"status": "uploaded", "index_state": "none", "chunk_count": 0,
                             "file_type": "application/pdf", "page_count": None, "error_message": None,
                             "embedding_model": None, "updated_at": "2026-01-01T00:00:00+00:00"},
                "conversations": {"title": "New conversation", "updated_at": "2026-01-01T00:00:00+00:00"},
                "messages": {"token_count": 0}}

    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.filters, self._limit = "select", None, [], None
        self.orders = []
        self._range = None

    def select(self, *_a):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict="id"):
        self.op, self.payload = "upsert", payload
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: r.get(col) >= val)
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: str(r.get(col)) == str(val))
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def or_(self, _expr):
        return self

    def order(self, col, desc=False):
        self.orders.append((col, desc))
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _visible(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.db.user_id and self.table in ("documents", "conversations", "processing_jobs", "document_chunks"):
            rows = [r for r in rows if r["user_id"] == self.db.user_id]
        return [r for r in rows if all(f(r) for f in self.filters)]

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "upsert":  # on id
            by_id = {r["id"]: r for r in rows}
            for p in self.payload:
                if p["id"] in by_id:
                    by_id[p["id"]].update(p)
                else:
                    rows.append({**p, "created_at": self.db.stamp()})
            return _Result(copy.deepcopy(self.payload))
        if self.op == "insert":
            new = []
            for p in (self.payload if isinstance(self.payload, list) else [self.payload]):
                row = {**self.DEFAULTS.get(self.table, {}), **p}
                row["id"] = p.get("id") or str(uuid.uuid4())
                row.setdefault("created_at", self.db.stamp())
                new.append(row)
            if self.table == "documents" and any(r["id"] == n["id"] for n in new for r in rows):
                raise Exception("duplicate key value violates unique constraint 23505")
            rows.extend(new)
            return _Result(copy.deepcopy(new))
        vis = self._visible()
        if self.op == "update":
            for r in vis:
                r.update(self.payload)
            return _Result(copy.deepcopy(vis))
        if self.op == "delete":
            for r in vis:
                rows.remove(r)
            return _Result([])
        for col, desc in reversed(self.orders):
            vis.sort(key=lambda r: (str(r.get(col)), r["id"]), reverse=desc)
        if self._range:
            vis = vis[self._range[0]:self._range[1] + 1]
        return _Result(copy.deepcopy(vis[:self._limit] if self._limit else vis))


_CLOCK = [0]


class FakeDB:
    def __init__(self, tables=None, user_id=None):
        self.tables, self.user_id = tables if tables is not None else {}, user_id

    @staticmethod
    def stamp():
        """Strictly increasing timestamps, like real inserts."""
        _CLOCK[0] += 1
        n = _CLOCK[0]
        return f"2026-01-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}+00:00"

    def table(self, name):
        return _Query(self, name)
