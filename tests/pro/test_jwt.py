import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings
from app.core.exceptions import Unauthorized
from app.core.security import JWKSCache, verify_token

URL = "https://proj.supabase.co"
USER = str(uuid.uuid4())


def _settings(secret=None):
    return Settings(URL, "anon", "svc", f"{URL}/auth/v1/.well-known/jwks.json", secret, "user-documents", "test")


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key, kid="k1"):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid=kid, alg="RS256", use="sig")
    return JWKSCache("u", fetch=lambda _u: {"keys": [jwk]})


def _token(key, kid="k1", alg="RS256", **over):
    claims = {"sub": USER, "iss": f"{URL}/auth/v1", "aud": "authenticated", "role": "authenticated",
              "exp": int(time.time()) + 600}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)


def test_valid_token(key):
    u = verify_token(_token(key), _settings(), _jwks(key))
    assert u.id == USER


@pytest.mark.parametrize("over", [
    {"exp": int(time.time()) - 3600},
    {"iss": "https://evil.example/auth/v1"},
    {"aud": "other"},
    {"role": "anon"},
    {"sub": "not-a-uuid"},
    {"sub": None},
    {"exp": None},
])
def test_rejected_claims(key, over):
    with pytest.raises(Unauthorized):
        verify_token(_token(key, **over), _settings(), _jwks(key))


def test_bad_signature(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(Unauthorized):
        verify_token(_token(other), _settings(), _jwks(key))


def test_unknown_kid(key):
    with pytest.raises(Unauthorized):
        verify_token(_token(key, kid="nope"), _settings(), _jwks(key))


def test_alg_none_rejected(key):
    t = jwt.encode({"sub": USER, "iss": f"{URL}/auth/v1", "aud": "authenticated", "role": "authenticated",
                    "exp": int(time.time()) + 60}, None, algorithm="none", headers={"kid": "k1"})
    with pytest.raises(Unauthorized):
        verify_token(t, _settings(), _jwks(key))


def test_hs256_with_kid_rejected_even_if_secret_configured(key):
    # algorithm-confusion guard: a token naming a kid must be asymmetric, whatever it is signed with
    t = jwt.encode({"sub": USER, "iss": f"{URL}/auth/v1", "aud": "authenticated", "role": "authenticated",
                    "exp": int(time.time()) + 60}, "s" * 32, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(Unauthorized):
        verify_token(t, _settings(secret="s" * 32), _jwks(key))


def test_hs256_only_without_kid_and_with_secret():
    secret = "s" * 32
    claims = {"sub": USER, "iss": f"{URL}/auth/v1", "aud": "authenticated", "role": "authenticated",
              "exp": int(time.time()) + 60}
    t = jwt.encode(claims, secret, algorithm="HS256")
    assert verify_token(t, _settings(secret=secret)).id == USER
    with pytest.raises(Unauthorized):  # secret not configured -> rejected
        verify_token(t, _settings())


@pytest.mark.parametrize("bad", ["", "garbage", "a.b.c"])
def test_malformed(bad):
    with pytest.raises(Unauthorized):
        verify_token(bad, _settings())
