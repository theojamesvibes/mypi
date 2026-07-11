"""Integration tests for app.main._ensure_encryption_key — the lifespan
startup step that resolves the Fernet key used for Pi-hole password
storage (env var → app_settings row → auto-generate + persist).

`settings` is a process-global pydantic-settings object and
app.models.pihole caches a module-level Fernet instance, so every test
here runs under `_preserve_key_state`, which restores *exactly* the
prior values afterwards. Other integration tests (e.g.
test_collector_polling's `instance` fixture) set
`settings.encryption_key` when it's empty and rely on it staying set —
leaking a different key (or clearing it) from here would break them.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

import app.models.pihole as pihole_models
from app.config import settings
from app.main import _ENCRYPTION_KEY_SETTING, _ensure_encryption_key
from app.models.settings import AppSetting


@pytest.fixture(autouse=True)
def _preserve_key_state():
    """Save and restore the process-global key state around each test."""
    saved_key = settings.encryption_key
    saved_fernet = pihole_models._fernet
    yield
    settings.encryption_key = saved_key
    pihole_models._fernet = saved_fernet


async def _app_setting_rows(db_session) -> list[AppSetting]:
    result = await db_session.execute(select(AppSetting))
    return list(result.scalars().all())


async def test_explicit_valid_key_returns_without_touching_db(db_session):
    """Priority 1: a valid ENCRYPTION_KEY from the environment is used
    as-is — no app_settings row is read or written."""
    key = Fernet.generate_key().decode()
    settings.encryption_key = key

    await _ensure_encryption_key()

    assert settings.encryption_key == key
    # _clean_db truncated everything before the test; if the function had
    # persisted anything, a row would exist now.
    assert await _app_setting_rows(db_session) == []


async def test_explicit_invalid_key_raises_runtime_error(db_session):
    """A malformed ENCRYPTION_KEY must abort startup loudly, with an
    error message that tells the operator how to generate a Fernet key."""
    settings.encryption_key = "definitely-not-a-fernet-key"

    with pytest.raises(RuntimeError, match="Fernet"):
        await _ensure_encryption_key()

    # The failure path must not persist anything either.
    assert await _app_setting_rows(db_session) == []


async def test_key_loaded_from_existing_db_row(db_session):
    """Priority 2: no env key + existing app_settings row → the stored
    key is loaded onto settings and the cached Fernet is invalidated so
    _get_fernet() re-initialises with the correct key."""
    db_key = Fernet.generate_key().decode()
    db_session.add(AppSetting(key=_ENCRYPTION_KEY_SETTING, value=db_key))
    await db_session.commit()

    settings.encryption_key = ""
    # Simulate a stale cached Fernet built from some previous key.
    pihole_models._fernet = Fernet(Fernet.generate_key())

    await _ensure_encryption_key()

    assert settings.encryption_key == db_key
    assert pihole_models._fernet is None


async def test_key_generated_and_persisted_when_absent(db_session):
    """Priority 3: no env key, no row → a fresh key is generated, saved
    to app_settings, set on settings, and is a usable Fernet key."""
    settings.encryption_key = ""
    pihole_models._fernet = Fernet(Fernet.generate_key())

    await _ensure_encryption_key()

    assert settings.encryption_key
    # Must construct without raising — i.e. a genuinely valid Fernet key.
    Fernet(settings.encryption_key.encode())

    row = await db_session.get(AppSetting, _ENCRYPTION_KEY_SETTING)
    assert row is not None
    assert row.value == settings.encryption_key
    assert pihole_models._fernet is None
