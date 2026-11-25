"""Supabase JWT verification: signature, exp, iss, aud, role, sub.

Asymmetric algorithms only (HS* and none are rejected), unless the optional legacy
SUPABASE_JWT_SECRET is set and the token carries no `kid`.
"""
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from app.core.config import get_settings
from app.core.exceptions import AppError, Unauthorized

logger = logging.getLogger("uvicorn.error")
ASYMMETRIC_ALGS = ["RS256", "ES256", "EdDSA"]
JWKS_TTL = 600
JWKS_MIN_REFRESH = 30  # seconds between refreshes triggered by unknown kids
LEEWAY = 30


@dataclass(frozen=True)
class AuthUser:
    id: str
    access_token: str = ""


class JWKSCache:
    """kid -> public key; refreshed on TTL expiry or unknown kid (rate-limited)."""

    def __init__(self, url: str, fetch=None):
        self.url, self._fetch = url, fetch
        self._keys, self._at, self._lock = {}, None, threading.Lock()

    def _load(self):
        import jwt
        if self._fetch:
            data = self._fetch(self.url)
        else:
            import httpx
            data = httpx.get(self.url, timeout=5).raise_for_status().json()
        self._keys = {k["kid"]: jwt.PyJWK(k).key for k in data.get("keys", []) if k.get("kid")}
        self._at = time.monotonic()

    def get(self, kid: str):
        with self._lock:
            age = None if self._at is None else time.monotonic() - self._at
            if age is None or age > JWKS_TTL or (kid not in self._keys and age > JWKS_MIN_REFRESH):
                try:
                    self._load()
                except Exception:
                    logger.warning("JWKS fetch failed")
                    if not self._keys:
                        raise
            return self._keys.get(kid)


_cache: Optional[JWKSCache] = None


def _jwks(settings) -> JWKSCache:
    global _cache
    if _cache is None or _cache.url != settings.jwks_url:
        _cache = JWKSCache(settings.jwks_url)
    return _cache


def verify_token(token: str, settings=None, jwks: Optional[JWKSCache] = None) -> AuthUser:
    import jwt  # lazy
    settings = settings or get_settings()
    try:
        header = jwt.get_unverified_header(token)
        alg, kid = header.get("alg"), header.get("kid")
        if kid:
            if alg not in ASYMMETRIC_ALGS:
                raise Unauthorized()
            key = (jwks or _jwks(settings)).get(kid)
            if key is None:
                raise Unauthorized()
            algs = ASYMMETRIC_ALGS
        elif alg == "HS256" and settings.jwt_secret:
            key, algs = settings.jwt_secret, ["HS256"]
        else:
            raise Unauthorized()
        claims = jwt.decode(token, key, algorithms=algs, issuer=settings.issuer, audience="authenticated",
                            leeway=LEEWAY, options={"require": ["exp", "sub", "iss", "aud"]})
        if claims.get("role") != "authenticated":  # rejects the anon key (itself a JWT)
            raise Unauthorized()
        return AuthUser(id=str(uuid.UUID(str(claims["sub"]))), access_token=token)
    except AppError:
        raise
    except Exception:  # invalid/expired token, bad sub, JWKS outage, malformed header
        raise Unauthorized() from None
