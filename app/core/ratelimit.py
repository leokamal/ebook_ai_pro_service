"""Tiny in-process sliding-window limiter (single uvicorn worker, so process-local state is correct).
Protects the paid/quota-limited resources (Gemini, embeddings) from one user hammering /chat."""
import os
import threading
import time
from collections import defaultdict, deque

from app.core.exceptions import AppError


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        if self.limit <= 0:  # disabled
            return
        now = self.clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                retry = max(1, int(self.window - (now - q[0])))
                raise AppError("rate_limited", "Too many requests; slow down", 429, {"Retry-After": str(retry)})
            q.append(now)
            if len(self._hits) > 10000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    del self._hits[k]


class ConcurrencyLimiter:
    """Caps in-flight streams per user. Each slot carries an expiry, so a slot leaked by an abnormal
    disconnect heals by itself instead of locking the user out."""

    def __init__(self, limit: int, ttl: float = 300.0, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self._slots = defaultdict(dict)
        self._lock = threading.Lock()

    def acquire(self, key: str):
        if self.limit <= 0:
            return None
        now = self.clock()
        with self._lock:
            slots = self._slots[key]
            for t in [t for t, exp in slots.items() if exp <= now]:
                del slots[t]
            if len(slots) >= self.limit:
                raise AppError("rate_limited", "Too many answers are being generated for you; wait for one to finish",
                               429, {"Retry-After": "5"})
            token = object()
            slots[token] = now + self.ttl
            return token

    def release(self, key: str, token) -> None:
        if token is None:
            return
        with self._lock:
            self._slots[key].pop(token, None)
            if not self._slots[key]:
                self._slots.pop(key, None)


_streams = {}


def stream_limiter() -> ConcurrencyLimiter:
    limit = int(os.environ.get("CHAT_MAX_CONCURRENT_STREAMS", 2))
    if _streams.get("limit") != limit:
        _streams.update(limit=limit, limiter=ConcurrencyLimiter(limit))
    return _streams["limiter"]


_limiters = {}


def get_limiter(name: str, env: str, default: int) -> SlidingWindowLimiter:
    """One limiter per endpoint family; rebuilt if the env value changes (tests, hot config)."""
    limit = int(os.environ.get(env, default))
    cur = _limiters.get(name)
    if cur is None or cur.limit != limit:
        cur = _limiters[name] = SlidingWindowLimiter(limit)
    return cur


def chat_limiter() -> SlidingWindowLimiter:
    return get_limiter("chat", "CHAT_RATE_PER_MINUTE", 20)


def search_limiter() -> SlidingWindowLimiter:
    return get_limiter("search", "SEARCH_RATE_PER_MINUTE", 60)
