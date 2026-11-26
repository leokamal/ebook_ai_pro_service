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


class LLMTimeout(LLMUnavailable):
    """Same public error as LLMUnavailable (clients depend on the code); only the logs tell them apart."""
    log_code = "llm_timeout"


@dataclass
class LLMResult:
    text: str
    usage: dict = field(default_factory=dict)  # {"input_tokens": int, "output_tokens": int}


Message = dict  # {"role": "user"|"assistant", "content": str}


class LLMProvider(Protocol):
    # `summarize` lets a provider trade reasoning depth for a faster first token
    def generate(self, system: str, messages: Sequence[Message], summarize: bool = False) -> LLMResult: ...
    def stream(self, system: str, messages: Sequence[Message],
               summarize: bool = False) -> Iterator[Union[str, dict]]: ...  # str chunks, then optional usage dict


def _is_quota(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("429", "resourceexhausted", "resource_exhausted", "quota", "rate limit"))


def _is_timeout(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("timeout", "timed out", "deadline"))


def _is_retryable(e: Exception) -> bool:
    """Timeouts are not retried: a retry would multiply a wait that is already long."""
    text = f"{type(e).__name__} {e}".lower()
    return not _is_timeout(e) and any(k in text for k in ("503", "502", "500", "unavailable", "connection",
                                                          "internalservererror", "serviceunavailable"))


def _summary_options(model: str) -> dict:
    """Less thinking for summaries. Unknown model families keep their defaults."""
    name = model.lower().removeprefix("models/")
    if name.startswith("gemini-3"):
        return {"thinking_level": "low"}
    if name.startswith("gemini-2.5-flash"):
        return {"thinking_budget": 0}
    return {}


class GeminiService:
    def __init__(self, api_key: str, model: str, timeout: float = 180.0, max_retries: int = 1, max_output_tokens: int = 1024,
                 model_factory: Optional[Callable[..., object]] = None, sleep: Callable[[float], None] = time.sleep, fallback_model: str = ""):
        self.model_name, self.timeout, self.max_retries = model, timeout, max_retries
        self.max_output_tokens = max_output_tokens  # bounds cost and stream length
        self.fallback_model = fallback_model if fallback_model != model else ""
        self._api_key, self._factory, self._sleep = api_key, model_factory, sleep
        self._models: dict = {}  # (model, summarize) -> client
        self._lock = threading.Lock()

    def _client(self, model: str, summarize: bool = False):
        with self._lock:
            key = (model, summarize)
            if key not in self._models:
                options = _summary_options(model) if summarize else {}
                if self._factory:
                    self._models[key] = self._factory(**options)
                else:
                    if not self._api_key:
                        logger.error("GEMINI_API_KEY is not configured")
                        raise LLMUnavailable()
                    from langchain_google_genai import ChatGoogleGenerativeAI  # lazy
                    # max_retries counts ATTEMPTS in this library: 1 = no hidden retry, we own retries
                    self._models[key] = ChatGoogleGenerativeAI(
                        model=model, google_api_key=self._api_key, timeout=self.timeout, max_retries=1,
                        max_output_tokens=self.max_output_tokens, **options)
            return self._models[key]

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
        if _is_timeout(e):
            logger.error("llm_timeout: no response from Gemini within %.0fs (%s)", self.timeout, type(e).__name__)
            return LLMTimeout()
        logger.exception("llm_unavailable: Gemini call failed")
        return LLMUnavailable()

    def _after_failure(self, e: Exception, model: str, attempt: int, can_retry: bool) -> tuple:
        """(model, attempt) to try next, or raise the mapped error. Only transient server/network errors are
        retried (never quota or timeouts), then handed to the fallback model if one is configured."""
        if not can_retry or _is_quota(e) or not _is_retryable(e):
            raise self._map(e) from None
        if attempt < self.max_retries:
            logger.warning("Gemini %s failed (%s); retrying", model, type(e).__name__)
            self._sleep(min(2 ** (attempt + 1), 8))
            return model, attempt + 1
        if self.fallback_model and model != self.fallback_model:
            logger.warning("Gemini %s unavailable; using fallback model %s", model, self.fallback_model)
            return self.fallback_model, 0
        raise self._map(e) from None

    def generate(self, system: str, messages: Sequence[Message], summarize: bool = False) -> LLMResult:
        model, attempt = self.model_name, 0
        while True:
            try:
                resp = self._client(model, summarize).invoke(self._to_lc(system, messages))
                usage = getattr(resp, "usage_metadata", None) or {}
                return LLMResult(self._text(resp.content), {"input_tokens": int(usage.get("input_tokens", 0)),
                                                            "output_tokens": int(usage.get("output_tokens", 0))})
            except AppError:
                raise
            except Exception as e:
                model, attempt = self._after_failure(e, model, attempt, can_retry=True)

    def stream(self, system: str, messages: Sequence[Message], summarize: bool = False) -> Iterator[str]:
        """Yields text chunks. Retries only while nothing has been sent (partial output cannot be taken back)."""
        model, attempt, sent = self.model_name, 0, False
        while True:
            usage = None
            try:
                for chunk in self._client(model, summarize).stream(self._to_lc(system, messages)):
                    text = self._text(chunk.content)
                    if text:
                        sent = True
                        yield text
                    usage = getattr(chunk, "usage_metadata", None) or usage
                if usage:  # final item: usage as a dict (the orchestrator tells it apart from text)
                    yield {"input_tokens": int(usage.get("input_tokens", 0)),
                           "output_tokens": int(usage.get("output_tokens", 0))}
                return
            except AppError:
                raise
            except Exception as e:
                model, attempt = self._after_failure(e, model, attempt, can_retry=not sent)
