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
    email: Optional[str] = None
    providers: tuple = ()
    name: Optional[str] = None        # from user_metadata: user-editable, DISPLAY ONLY
    avatar_url: Optional[str] = None  # idem


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


def _text(value, limit: int = 500) -> Optional[str]:
    return value[:limit] if isinstance(value, str) and value else None


def _user_from_claims(claims: dict, token: str, settings) -> AuthUser:
    """Runs only after the signature and standard claims were verified."""
    if claims.get("is_anonymous"):  # anonymous sign-ins also carry role=authenticated
        raise Unauthorized()
    app_meta = claims.get("app_metadata") if isinstance(claims.get("app_metadata"), dict) else {}
    listed = app_meta.get("providers") if isinstance(app_meta.get("providers"), list) else []
    providers = tuple(dict.fromkeys(p for p in [app_meta.get("provider"), *listed] if isinstance(p, str) and p))
    # app_metadata is server-controlled; user_metadata is NOT and is never used for decisions
    if settings.allowed_providers and not set(providers) & set(settings.allowed_providers):
        raise AppError("provider_not_allowed", "Sign in with a supported provider", 403)
    meta = claims.get("user_metadata") if isinstance(claims.get("user_metadata"), dict) else {}
    return AuthUser(id=str(uuid.UUID(str(claims["sub"]))), access_token=token, email=_text(claims.get("email")),
                    providers=providers, name=_text(meta.get("full_name") or meta.get("name"), 200),
                    avatar_url=_text(meta.get("avatar_url") or meta.get("picture"), 1000))


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
        return _user_from_claims(claims, token, settings)
    except AppError:
        raise
    except Exception:  # invalid/expired token, bad sub, JWKS outage, malformed header
        raise Unauthorized() from None
