"""Tests for app/services/pushover.py — the encrypt/decrypt round-trip
that protects creds at rest, the masking helper, and alert filtering."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _ensure_fernet_key():
    """Fernet encryption needs an active key — ensure one exists before
    any encrypt/decrypt call."""
    from app.config import settings
    import app.models.pihole as pihole_models

    if not settings.encryption_key:
        settings.encryption_key = Fernet.generate_key().decode()
        pihole_models._fernet = None
    yield


@pytest.fixture(autouse=True)
def _reset_pushover_module(monkeypatch):
    from app.services import pushover
    monkeypatch.setattr(pushover, "_app_token", "")
    monkeypatch.setattr(pushover, "_user_key", "")
    monkeypatch.setattr(pushover, "_enabled", False)
    yield


# ── Encrypt / decrypt round-trip ─────────────────────────────────────────────


def test_encrypt_then_decrypt_recovers_plaintext():
    from app.services.pushover import _decrypt, _encrypt
    plaintext = "azGDORePK8gMaC0QOYAMyEEuzJnyUi"
    cipher = _encrypt(plaintext)
    assert cipher != plaintext  # ciphertext should differ
    assert _decrypt(cipher) == plaintext


def test_encrypt_passes_empty_through():
    """Empty values short-circuit so we never store an empty Fernet
    token (which would still be valid bytes but pointless)."""
    from app.services.pushover import _encrypt
    assert _encrypt("") == ""


def test_decrypt_treats_legacy_plaintext_as_passthrough():
    """Settings written before encryption was added are stored as
    plain text. _decrypt returns them as-is so the next save_settings
    re-encrypts them — the migration is silent."""
    from app.services.pushover import _decrypt
    legacy = "this-is-plaintext-not-fernet"
    assert _decrypt(legacy) == legacy


# ── Mask helper ──────────────────────────────────────────────────────────────


def test_mask_returns_last_4_chars():
    from app.services.pushover import _mask
    assert _mask("0123456789abcdef") == "****cdef"


def test_mask_returns_empty_for_unset():
    from app.services.pushover import _mask
    assert _mask("") == ""


# ── Alert filtering ──────────────────────────────────────────────────────────


async def test_send_returns_false_when_not_enabled():
    """Even with valid credentials in memory, send() short-circuits
    if `enabled` is False — guards against accidentally pushing alerts
    while a feature flag is off."""
    from app.services import pushover
    pushover._app_token = "tok"
    pushover._user_key = "key"
    pushover._enabled = False
    assert await pushover.send("body") is False


async def test_send_returns_false_when_credentials_missing():
    from app.services import pushover
    pushover._app_token = ""
    pushover._user_key = ""
    pushover._enabled = True
    assert await pushover.send("body") is False


async def test_notify_sync_failure_skipped_when_alert_disabled(monkeypatch):
    """notify_sync_failure honours the `alert_sync_failure` toggle and
    returns without contacting Pushover."""
    from app.services import pushover

    pushover._enabled = True
    pushover._app_token = "tok"
    pushover._user_key = "key"
    monkeypatch.setattr(pushover, "_alert_sync_failure", False)

    sent = []

    async def _capture(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(pushover, "send", _capture)
    await pushover.notify_sync_failure("boom")

    assert sent == []


async def test_notify_sync_failure_sends_when_enabled(monkeypatch):
    from app.services import pushover

    pushover._enabled = True
    pushover._app_token = "tok"
    pushover._user_key = "key"
    monkeypatch.setattr(pushover, "_alert_sync_failure", True)

    sent = []

    async def _capture(*args, **kwargs):
        sent.append(kwargs.get("title"))
        return True

    monkeypatch.setattr(pushover, "send", _capture)
    await pushover.notify_sync_failure("boom")

    assert "MyPi Sync Error" in sent
