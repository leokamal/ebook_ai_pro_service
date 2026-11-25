"""Small, pure RAG building blocks: memory, context, reranking, citations. No I/O, easy to test."""
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from app.prompts.rag import NOT_FOUND, SOURCES_CLOSE, SOURCES_OPEN
from app.services.vector_store import SearchHit


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ------------------------------------------------------------------------------- memory
class ConversationMemoryService:
    """Last N messages, trimmed oldest-first to a token budget. History is chat text, never document text."""

    def __init__(self, max_messages: int = 6, max_tokens: int = 1500):
        self.max_messages, self.max_tokens = max_messages, max_tokens

    def select(self, messages: Sequence[dict]) -> list[dict]:
        recent = [m for m in messages if m["role"] in ("user", "assistant")][-self.max_messages:]
        kept, budget = [], self.max_tokens
        for m in reversed(recent):
            cost = approx_tokens(m["content"])
            if cost > budget and kept:
                break
            # labels renumber every turn: a stale [S2] in history must not be copied as a valid citation
            kept.append({"role": m["role"], "content": _MARKER.sub("", m["content"])[:4000].strip()})
            budget -= cost
        return list(reversed(kept))


# ------------------------------------------------------------------------------- reranking
_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set:
    return set(_WORD.findall(text.lower()))


class RerankingService:
    """MVP reranker (no cross-encoder: RAM). Score cut-off, near-duplicate removal, per-document cap.
    Replace with a cross-encoder behind the same `rerank` signature later."""

    def __init__(self, min_score: Optional[float] = None, max_per_doc: int = 3, dup_threshold: float = 0.9):
        self.min_score, self.max_per_doc, self.dup = min_score, max_per_doc, dup_threshold

    def rerank(self, hits: Sequence[SearchHit], top_n: int, per_doc_cap: bool = True) -> list[SearchHit]:
        kept, per_doc, seen = [], {}, []
        for h in sorted(hits, key=lambda h: (-h.score, h.chunk_id)):
            if self.min_score is not None and h.score < self.min_score:
                continue
            if per_doc_cap and per_doc.get(h.document_id, 0) >= self.max_per_doc:
                continue
            toks = _tokens(h.text)
            if any(toks and s and len(toks & s) / len(toks | s) >= self.dup for s in seen):
                continue
            kept.append(h)
            seen.append(toks)
            per_doc[h.document_id] = per_doc.get(h.document_id, 0) + 1
            if len(kept) >= top_n:
                break
        return kept


# ------------------------------------------------------------------------------- context
_TAG = re.compile(r"<\s*(/?)\s*sources", re.I)
MAX_CHUNK_CHARS = 2000


def _clean_title(title: str) -> str:
    return re.sub(r'["\r\n<>]+', " ", title).strip()[:120]


class ContextBuilder:
    def __init__(self, max_chars: int = 12000):
        self.max_chars = max_chars

    def _fit(self, hits: Sequence[SearchHit]) -> list[SearchHit]:
        kept, total = [], 0
        for h in hits:
            cost = min(len(h.text), MAX_CHUNK_CHARS) + 120
            if kept and total + cost > self.max_chars:
                break
            kept.append(h)
            total += cost
        return kept

    def build(self, hits: Sequence[SearchHit], group: bool = False) -> tuple[str, list[SearchHit]]:
        """Returns (<sources> block, the hits actually included, in label order: hits[0] is [S1]).

        `hits` come best-first and the char budget keeps the best ones. With `group=True` (multi-document)
        the kept hits are then laid out document by document (documents ordered by their best score, chunks in
        reading order) so the model sees one coherent passage per book; labels follow that final order."""
        if group:
            hits = self._fit(hits)
            best = {}
            for h in hits:
                best[h.document_id] = max(best.get(h.document_id, 0.0), h.score)
            hits = sorted(hits, key=lambda h: (-best[h.document_id], h.document_id, h.chunk_index))
        parts, used, total = [], [], 0
        for h in hits:
            body = _TAG.sub(lambda m: "‹" + m.group(1) + "sources", h.text[:MAX_CHUNK_CHARS])  # a chunk can never open/close the block
            entry = f'[S{len(used) + 1}] (document: "{_clean_title(h.document_title)}", page {h.page_number})\n{body}'
            if used and total + len(entry) > self.max_chars:
                break
            parts.append(entry)
            used.append(h)
            total += len(entry)
        return f"{SOURCES_OPEN}\n" + "\n\n".join(parts) + f"\n{SOURCES_CLOSE}", used


# ------------------------------------------------------------------------------- citations
_MARKER = re.compile(r"\[\s*S(\d+(?:\s*[,;]\s*S?\d+)*)\s*\]")


@dataclass
class Citation:
    chunk_id: str
    document_id: str
    document_title: str
    page_number: int
    relevance_score: float
    snippet: str


class CitationService:
    """Sources come ONLY from [Sn] markers that map to chunks that were really in the prompt."""

    def process(self, answer: str, used: Sequence[SearchHit]) -> tuple[str, list[Citation]]:
        order: list[int] = []

        def sub(m):
            nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
            valid = [n for n in nums if 1 <= n <= len(used)]
            for n in valid:
                if n not in order:
                    order.append(n)
            return "".join(f"[S{n}]" for n in valid)  # unknown labels are stripped from the text

        text = re.sub(r"[ \t]+([.,;:!?])", r"\1", _MARKER.sub(sub, answer))
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        cites = [Citation(h.chunk_id, h.document_id, h.document_title, h.page_number, round(h.score, 4),
                          h.text[:200]) for h in (used[n - 1] for n in order)]
        return text, cites


# ------------------------------------------------------------------------------- language of fixed replies
_FR = re.compile(r"[àâçéèêëîïôûùüÿœ]|\b(le|la|les|des|est|quel|quelle|que|qui|dans|pour|et|une?)\b", re.I)


def guess_language(text: str) -> str:
    if re.search(r"[؀-ۿ]", text):
        return "ar"
    return "fr" if _FR.search(text) else "en"


def not_found_message(question: str) -> str:
    return NOT_FOUND[guess_language(question)]
