"""Shared builders for ingestion / runner / API tests."""
import uuid

from app.repositories.documents import ChunkRepository, DocumentAdminRepository
from app.services.embedding import HuggingFaceEmbedder
from app.services.ingestion import IngestConfig, IngestionService
from app.services.vector_store import ChromaVectorStore
from tests.pro.fakechroma import FakeChromaClient
from tests.pro.fakejobs import FakeServiceDB

A, B = str(uuid.uuid4()), str(uuid.uuid4())
MODEL, DIM = "fake-model", 4
PDF = b"%PDF-1.7 fake"


class FakeModel:
    AXES = ["cat", "dog", "car", "sun"]

    def __init__(self, fail_after=None):
        self.calls, self.fail_after = 0, fail_after

    def get_sentence_embedding_dimension(self):
        return DIM

    def encode(self, texts, batch_size, normalize_embeddings, show_progress_bar):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("embedding backend down")
        return [[1.0 if ax in t.lower() else 0.01 for ax in self.AXES] for t in texts]


class FakeStorage:
    def __init__(self):
        self.files, self.removed, self.fail_download = {}, [], False
        self.ages = {}  # path -> age in hours

    def put(self, user, doc, name, data=PDF):
        self.files[f"{user}/{doc}/{name}"] = data

    def inspect_object(self, bucket, path):
        data = self.files.get(path)
        return None if data is None else (len(data), data[:8])

    def download_to_path(self, bucket, path, dest, max_bytes):
        if self.fail_download:
            raise ConnectionError("storage down")
        data = self.files[path]
        if len(data) > max_bytes:
            raise ValueError("too big")
        with open(dest, "wb") as f:
            f.write(data)
        return len(data)

    def remove_object(self, bucket, path):
        self.removed.append(path)
        self.files.pop(path, None)


class FakePdf:
    def __init__(self, pages):
        self.pages, self.page_count, self.closed = pages, len(pages), False

    def close(self):
        self.closed = True


def make_opener(pages):
    return lambda path, max_pages: (FakePdf(pages), len(pages))


def fake_iter_pages(pdf):
    for i, t in enumerate(pdf.pages):
        yield i + 1, t


def fake_split(text):
    return [text[i:i + 20] for i in range(0, len(text), 20)] if text.strip() else []


def doc_row(user, doc, name="b.pdf", size=len(PDF), status="uploaded", **extra):
    return {"id": doc, "user_id": user, "title": "Book", "file_name": name, "file_path": f"{user}/{doc}/{name}",
            "file_size": size, "language": None, "status": status, "index_state": "none", "chunk_count": 0,
            "page_count": None, "error_message": None, "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00", "file_type": "application/pdf",
            "embedding_model": None, **extra}


class World:
    """One shared fake Postgres + storage + chroma, with an ingestion service on top."""

    def __init__(self, pages=("cat sits on the mat. " * 3, "dog runs fast. " * 3), model=None):
        self.tables = {}
        self.db = FakeServiceDB(self.tables)
        self.storage = FakeStorage()
        self.db.storage = self.storage
        self.client = FakeChromaClient()
        self.store = ChromaVectorStore("p", "col", MODEL, DIM, batch_size=2, client_factory=lambda _p: self.client)
        self.model = model or FakeModel()
        self.pages = list(pages)
        self.docs = DocumentAdminRepository(self.db)
        self.chunks = ChunkRepository(self.db)
        self.service = self.make_service()

    def make_service(self, model=None, pages=None):
        embedder = HuggingFaceEmbedder(MODEL, DIM, batch_size=2, model_factory=lambda _n: model or self.model)
        return IngestionService(self.docs, self.chunks, self.storage, embedder, self.store,
                                IngestConfig(bucket="user-documents", embed_batch=2),
                                open_pdf=make_opener(pages if pages is not None else self.pages),
                                splitter=fake_split, iter_pages=fake_iter_pages)

    def add_doc(self, user=A, **kw):
        doc = str(uuid.uuid4())
        row = doc_row(user, doc, **kw)
        self.tables.setdefault("documents", []).append(row)
        self.storage.put(user, doc, row["file_name"])
        return doc
