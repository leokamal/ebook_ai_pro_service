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
              "exp": int(time.time()) + 600, "app_metadata": {"provider": "google", "providers": ["google"]}}
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
    claims = {"app_metadata": {"provider": "google"}, "sub": USER, "iss": f"{URL}/auth/v1", "aud": "authenticated", "role": "authenticated",
              "exp": int(time.time()) + 60}
    t = jwt.encode(claims, secret, algorithm="HS256")
    assert verify_token(t, _settings(secret=secret)).id == USER
    with pytest.raises(Unauthorized):  # secret not configured -> rejected
        verify_token(t, _settings())


@pytest.mark.parametrize("bad", ["", "garbage", "a.b.c"])
def test_malformed(bad):
    with pytest.raises(Unauthorized):
        verify_token(bad, _settings())


# ---------------------------------------------------------------- sign-in providers
def _verify(key, **over):
    return verify_token(_token(key, **over), _settings(), _jwks(key))


@pytest.mark.parametrize("meta", [{"provider": "google"}, {"provider": "facebook", "providers": ["facebook"]},
                                  {"provider": "email", "providers": ["email", "google"]}])
def test_google_and_facebook_accounts_are_accepted(key, meta):
    assert _verify(key, app_metadata=meta).id == USER


def test_profile_claims_are_exposed_for_display(key):
    u = _verify(key, email="a@x.test", user_metadata={"full_name": "Ana", "picture": "https://p"})
    assert (u.email, u.name, u.avatar_url, u.providers) == ("a@x.test", "Ana", "https://p", ("google",))


def test_anonymous_sign_in_is_unauthorized(key):
    with pytest.raises(Unauthorized):
        _verify(key, is_anonymous=True)


@pytest.mark.parametrize("over", [{"app_metadata": {"provider": "email", "providers": ["email"]}},
                                  {"app_metadata": None},
                                  {"app_metadata": None, "user_metadata": {"provider": "google"}}])  # forged
def test_other_providers_are_forbidden(key, over):
    from app.core.exceptions import AppError
    with pytest.raises(AppError) as e:
        _verify(key, **over)
    assert e.value.status_code == 403 and e.value.code == "provider_not_allowed"


def test_empty_allow_list_accepts_any_provider_and_env_parsing(key, monkeypatch):
    import dataclasses
    from app.core.config import get_settings
    open_ = dataclasses.replace(_settings(), allowed_providers=())
    assert verify_token(_token(key, app_metadata=None), open_, _jwks(key)).providers == ()
    monkeypatch.delenv("AUTH_ALLOWED_PROVIDERS", raising=False)
    assert get_settings().allowed_providers == ("google", "facebook")
    monkeypatch.setenv("AUTH_ALLOWED_PROVIDERS", "")
    assert get_settings().allowed_providers == ()
    monkeypatch.setenv("AUTH_ALLOWED_PROVIDERS", "Google, apple")
    assert get_settings().allowed_providers == ("google", "apple")
