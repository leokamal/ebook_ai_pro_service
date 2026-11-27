"""In-memory stand-in for chromadb (client + collection) with the filter operators we use."""
import math


def _match(meta, where):
    if "$and" in where:
        return all(_match(meta, w) for w in where["$and"])
    ((k, cond),) = where.items()
    v = meta.get(k)
    if isinstance(cond, dict):
        ((op, arg),) = cond.items()
        return {"$in": lambda: v in arg, "$ne": lambda: v != arg, "$nin": lambda: v not in arg, "$eq": lambda: v == arg}[op]()
    return v == cond


def _cos_dist(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return 1.0 - dot / (na * nb) if na and nb else 1.0


class FakeCollection:
    def __init__(self, name, metadata):
        self.name, self.metadata = name, dict(metadata or {})
        self.rows = {}  # id -> (embedding, document, metadata)
        self.ignore_where_on_query = False  # simulate a broken/tampered filter

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            self.rows[i] = (e, d, dict(m))

    def delete(self, where):
        for i in [i for i, (_, _, m) in self.rows.items() if _match(m, where)]:
            del self.rows[i]

    def get(self, where=None, include=None):
        return {"ids": [i for i, (_, _, m) in self.rows.items() if where is None or _match(m, where)]}

    def count(self):
        return len(self.rows)

    def query(self, query_embeddings, n_results, where, include):
        q = query_embeddings[0]
        pool = [(i, r) for i, r in self.rows.items() if self.ignore_where_on_query or _match(r[2], where)]
        pool.sort(key=lambda x: _cos_dist(q, x[1][0]))
        pool = pool[:n_results]
        return {"ids": [[i for i, _ in pool]], "documents": [[r[1] for _, r in pool]],
                "metadatas": [[r[2] for _, r in pool]], "distances": [[_cos_dist(q, r[0]) for _, r in pool]]}


class FakeChromaClient:
    """Shared across 'restarts' of a store to emulate persistence."""

    def __init__(self):
        self.cols = {}

    fail_delete = False

    def get_collection(self, name):
        if name not in self.cols:
            raise ValueError(f"Collection {name} does not exist.")
        return self.cols[name]

    def create_collection(self, name, metadata=None):
        if name in self.cols:
            raise ValueError("already exists")
        self.cols[name] = FakeCollection(name, metadata)
        return self.cols[name]

    def delete_collection(self, name):
        if self.fail_delete:
            raise RuntimeError("io error")
        if name not in self.cols:
            raise ValueError(f"Collection {name} does not exist.")
        del self.cols[name]
