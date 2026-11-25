"""PDF text extraction (PyMuPDF, imported lazily) + text normalization + per-page chunking."""
import re
import unicodedata
from typing import Iterator

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t ]+")
_HYPHEN = re.compile(r"(?<=[A-Za-zÀ-ɏ])-\n(?=[a-zà-ɏ])")  # Latin only: never touch Arabic
_BLANKS = re.compile(r"\n{3,}")


class IngestError(Exception):
    """Permanent failure: retrying will not help. `code` is a stable identifier, `message` is user-safe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # also folds Arabic presentation forms
    text = _CTRL.sub("", text)
    text = _HYPHEN.sub("", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def open_pdf(path: str, max_pages: int):
    """Returns (doc, page_count). Corrupt, encrypted and oversized files are permanent errors."""
    import fitz  # PyMuPDF, lazy
    try:
        doc = fitz.open(path)
    except Exception:
        raise IngestError("corrupt_pdf", "The PDF is corrupted or unreadable") from None
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise IngestError("encrypted_pdf", "Password-protected PDFs are not supported")
    if doc.page_count > max_pages:
        n = doc.page_count
        doc.close()
        raise IngestError("too_many_pages", f"The PDF has {n} pages (limit {max_pages})")
    return doc, doc.page_count


def iter_pages(doc) -> Iterator[tuple[int, str]]:
    """Yield (1-based page number, normalized text), one page at a time (bounded memory)."""
    for i in range(doc.page_count):
        try:
            raw = doc.load_page(i).get_text("text")
        except Exception:
            raise IngestError("corrupt_pdf", "The PDF is corrupted or unreadable") from None
        yield i + 1, normalize_text(raw)


_splitter = None


def split_page(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Split ONE page, so every chunk maps to exactly one page number."""
    global _splitter
    if not text:
        return []
    if _splitter is None:
        from langchain_text_splitters import RecursiveCharacterTextSplitter  # lazy
        _splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    return [c for c in _splitter.split_text(text) if c.strip()]
