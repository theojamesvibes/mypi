"""Unit tests for app/auth.py — covers the security-critical pure
functions that wave-1, wave-2, and wave-3 hardening touched."""
from __future__ import annotations

import bcrypt
import jwt
import pytest

from app import auth
from app.config import settings

# ── bcrypt password hashing ──────────────────────────────────────────────────


def test_hash_password_round_trip():
    h = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", h) is True
    assert auth.verify_password("wrong", h) is False


def test_hash_password_truncates_at_72_bytes():
    """bcrypt 5.0 raises on >72-byte input; we truncate to preserve the
    4.x silent-truncation behaviour so existing stored hashes verify."""
    long_pw = "a" * 100
    h = auth.hash_password(long_pw)
    # Both the full 100-byte password AND a 72-byte truncation verify
    # against the same hash, because the hash was made from the truncated
    # bytes and verify_password truncates the same way before checking.
    assert auth.verify_password(long_pw, h) is True
    assert auth.verify_password("a" * 72, h) is True
    # 73rd byte of the input is irrelevant to the hash.
    assert auth.verify_password("a" * 72 + "b", h) is True


def test_verify_password_rejects_wrong_password():
    h = auth.hash_password("correct")
    assert auth.verify_password("incorrect", h) is False


# ── verify_user_password — login timing-equalisation ─────────────────────────


class _StubUser:
    """Minimal stand-in for app.models.user.User — only the attribute
    verify_user_password reads."""

    def __init__(self, hashed: str):
        self.hashed_password = hashed


def test_verify_user_password_returns_true_for_match():
    user = _StubUser(auth.hash_password("hunter2"))
    assert auth.verify_user_password("hunter2", user) is True


def test_verify_user_password_returns_false_for_mismatch():
    user = _StubUser(auth.hash_password("hunter2"))
    assert auth.verify_user_password("wrong", user) is False


def test_verify_user_password_returns_false_for_none_user():
    """The dummy-hash path always fails, but it must run bcrypt to
    equalise response time vs. the real-user path."""
    assert auth.verify_user_password("anything", None) is False


def test_equalize_login_timing_runs_without_raising():
    """The lockout branch calls this instead of a real password check;
    it must burn a bcrypt verify quietly (no return value, no exception)."""
    assert auth.equalize_login_timing() is None


def test_dummy_bcrypt_hash_is_a_real_bcrypt_hash():
    """If the dummy hash isn't a valid $2b$ string, bcrypt.checkpw raises
    instead of returning False — which would break the timing assumption
    on the username-not-found branch and surface as a 500 instead of a 401."""
    assert auth._DUMMY_BCRYPT_HASH.startswith("$2b$")
    # Calling checkpw against the dummy must return False without raising.
    assert bcrypt.checkpw(b"anything", auth._DUMMY_BCRYPT_HASH.encode()) is False


# ── JWT algorithm allowlist ──────────────────────────────────────────────────


@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512"])
def test_jwt_algorithm_accepts_hmac_family(alg, monkeypatch):
    monkeypatch.setattr(settings, "algorithm", alg)
    assert auth._jwt_algorithm() == alg


@pytest.mark.parametrize("bad", ["none", "None", "RS256", "ES256", "", "garbage"])
def test_jwt_algorithm_falls_back_for_invalid(bad, monkeypatch, caplog):
    monkeypatch.setattr(settings, "algorithm", bad)
    with caplog.at_level("WARNING"):
        result = auth._jwt_algorithm()
    assert result == "HS256"
    # The warning is the operator's signal that their config is wrong.
    assert any("not in the supported set" in rec.message for rec in caplog.records)


# ── JWT round-trip ───────────────────────────────────────────────────────────


def test_create_and_decode_token_round_trip():
    token = auth.create_access_token("alice", expire_minutes=60)
    claims = auth._decode_token_claims(token)
    assert claims is not None
    assert claims["sub"] == "alice"
    assert "jti" in claims
    assert "exp" in claims


def test_decode_token_returns_none_for_garbage():
    assert auth._decode_token_claims("not.a.jwt") is None


def test_decode_token_rejects_none_algorithm_token():
    """Even if an attacker hand-crafts a token with alg=none, the
    decode allow-list refuses to accept it.

    PyJWT refuses to *encode* with alg=none unless the key is None, so we
    forge the token by hand: base64url(header).base64url(payload).<empty
    signature>.
    """
    import base64
    import json

    def _b64u(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _b64u({"alg": "none", "typ": "JWT"})
    payload = _b64u({"sub": "alice", "exp": 9999999999, "jti": "x"})
    forged = f"{header}.{payload}."
    assert auth._decode_token_claims(forged) is None


def test_decode_token_rejects_wrong_signature(monkeypatch):
    """A token signed with a different secret must not validate."""
    # 32+ bytes so PyJWT's InsecureKeyLengthWarning stays out of the run.
    token = jwt.encode(
        {"sub": "alice", "exp": 9999999999, "jti": "x"},
        key="some-other-secret-that-is-long-enough",
        algorithm="HS256",
    )
    assert auth._decode_token_claims(token) is None


# ── API-key hashing ──────────────────────────────────────────────────────────


def test_hash_api_key_is_deterministic():
    h1 = auth.hash_api_key("raw-key-abc")
    h2 = auth.hash_api_key("raw-key-abc")
    assert h1 == h2


def test_hash_api_key_changes_with_input():
    assert auth.hash_api_key("a") != auth.hash_api_key("b")


def test_hash_api_key_uses_hmac_not_plain_sha256():
    """Hash-key path must be keyed by api_key_salt — not raw SHA-256.
    Guard against an accidental swap-back to the legacy hash function."""
    assert auth.hash_api_key("x") != auth._hash_api_key_sha256_legacy("x")


def test_generate_api_key_returns_consistent_hash():
    raw, h = auth.generate_api_key()
    assert auth.hash_api_key(raw) == h
    # secrets.token_urlsafe(32) → roughly 43 chars of url-safe base64.
    assert len(raw) >= 40


def test_legacy_sha256_helper_is_plain_unkeyed():
    """Sanity: legacy helper produces the same value as a stdlib SHA-256
    so the hash-upgrade migration path stays compatible with whatever
    pre-1.4.0 keys are still in the wild."""
    import hashlib
    expected = hashlib.sha256(b"raw-key").hexdigest()
    assert auth._hash_api_key_sha256_legacy("raw-key") == expected
