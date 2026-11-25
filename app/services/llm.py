"""LLM provider abstraction + Gemini implementation (blocking; call from `def` routes or threads).

Replaceable: the RAG code depends only on `LLMProvider`. Gemini specifics (langchain_google_genai, error
shapes, usage metadata) stay in this module and are imported lazily.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Protocol, Sequence, Union

from app.core.exceptions import AppError

logger = logging.getLogger("uvicorn.error")


class LLMQuotaError(AppError):
    def __init__(self):
        super().__init__("llm_quota_exceeded", "The AI service quota is exhausted; try again later", 429,
                         {"Retry-After": "60"})


class LLMUnavailable(AppError):
    def __init__(self):
        super().__init__("llm_unavailable", "The AI service is temporarily unavailable", 503)


@dataclass
class LLMResult:
    text: str
    usage: dict = field(default_factory=dict)  # {"input_tokens": int, "output_tokens": int}


Message = dict  # {"role": "user"|"assistant", "content": str}


class LLMProvider(Protocol):
    def generate(self, system: str, messages: Sequence[Message]) -> LLMResult: ...
    def stream(self, system: str, messages: Sequence[Message]) -> Iterator[Union[str, dict]]: ...  # str chunks, then optional usage dict


def _is_quota(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("429", "resourceexhausted", "resource_exhausted", "quota", "rate limit"))


def _is_retryable(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("timeout", "timed out", "503", "502", "500", "unavailable", "deadline",
                                   "connection", "internalservererror", "serviceunavailable"))


class GeminiService:
    def __init__(self, api_key: str, model: str, timeout: float = 60.0, max_retries: int = 2, max_output_tokens: int = 1024,
                 model_factory: Optional[Callable[[], object]] = None, sleep: Callable[[float], None] = time.sleep):
        self.model_name, self.timeout, self.max_retries = model, timeout, max_retries
        self.max_output_tokens = max_output_tokens  # bounds cost and stream length
        self._api_key, self._factory, self._sleep = api_key, model_factory, sleep
        self._model = None
        self._lock = threading.Lock()

    def _client(self):
        with self._lock:
            if self._model is None:
                if self._factory:
                    self._model = self._factory()
                else:
                    if not self._api_key:
                        logger.error("GEMINI_API_KEY is not configured")
                        raise LLMUnavailable()
                    from langchain_google_genai import ChatGoogleGenerativeAI  # lazy
                    self._model = ChatGoogleGenerativeAI(model=self.model_name, google_api_key=self._api_key,
                                                         timeout=self.timeout, max_retries=0,  # we own retries
                                                         max_output_tokens=self.max_output_tokens)
            return self._model

    @staticmethod
    def _to_lc(system: str, messages: Sequence[Message]):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        out = [SystemMessage(content=system)]
        out += [(HumanMessage if m["role"] == "user" else AIMessage)(content=m["content"]) for m in messages]
        return out

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
        return ""

    def _map(self, e: Exception) -> AppError:
        if _is_quota(e):
            logger.warning("Gemini quota/rate limit hit")
            return LLMQuotaError()
        logger.exception("Gemini call failed")
        return LLMUnavailable()

    def generate(self, system: str, messages: Sequence[Message]) -> LLMResult:
        attempt = 0
        while True:
            try:
                resp = self._client().invoke(self._to_lc(system, messages))
                usage = getattr(resp, "usage_metadata", None) or {}
                return LLMResult(self._text(resp.content), {"input_tokens": int(usage.get("input_tokens", 0)),
                                                            "output_tokens": int(usage.get("output_tokens", 0))})
            except AppError:
                raise
            except Exception as e:
                if _is_quota(e) or not _is_retryable(e) or attempt >= self.max_retries:
                    raise self._map(e) from None  # quota is never retried inside the request
                attempt += 1
                self._sleep(min(2 ** attempt, 8))

    def stream(self, system: str, messages: Sequence[Message]) -> Iterator[str]:
        """Yields text chunks. No mid-stream retry (partial output was already sent)."""
        usage = None
        try:
            for chunk in self._client().stream(self._to_lc(system, messages)):
                text = self._text(chunk.content)
                if text:
                    yield text
                usage = getattr(chunk, "usage_metadata", None) or usage
            if usage:  # final item: usage as a dict (the orchestrator tells it apart from text)
                yield {"input_tokens": int(usage.get("input_tokens", 0)), "output_tokens": int(usage.get("output_tokens", 0))}
        except AppError:
            raise
        except Exception as e:
            raise self._map(e) from None
