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


# ── scripts/rotate_encryption_key.py ─────────────────────────────────────────
#
# The rotation script moves the key out of app_settings: everything Fernet-
# encrypted (pihole passwords, pushover creds) is re-encrypted under a fresh
# key and the DB copy of the old key is deleted. scripts/ is not a package,
# so load it by path.


def _load_rotation_module():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[2] / "scripts" / "rotate_encryption_key.py"
    spec = importlib.util.spec_from_file_location("rotate_encryption_key", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_rotation_reencrypts_everything_and_deletes_db_key(db_session):
    import json

    from sqlalchemy import text

    rot = _load_rotation_module()

    old_key = Fernet.generate_key().decode()
    old = Fernet(old_key.encode())
    settings.encryption_key = ""  # force _resolve_old_key onto the DB row
    db_session.add(AppSetting(key=_ENCRYPTION_KEY_SETTING, value=old_key))

    # A site with one instance whose password is encrypted under the old key,
    # plus a pushover settings row with encrypted creds and one legacy
    # plaintext field (pre-encryption row).
    from app.models.site import Site, SiteSetting

    site = Site(name="Rot", slug="rot", is_main=True)
    db_session.add(site)
    await db_session.flush()
    await db_session.execute(
        text(
            "INSERT INTO pihole_instances (id, site_id, name, url, api_password, color, is_active, is_master) "
            "VALUES (gen_random_uuid(), :sid, 'ph1', 'http://ph1', :pw, '#fff', true, true)"
        ),
        {"sid": site.id, "pw": old.encrypt(b"hunter2").decode()},
    )
    db_session.add(
        SiteSetting(
            site_id=site.id,
            key="pushover_settings",
            value=json.dumps(
                {
                    "app_token": old.encrypt(b"tok").decode(),
                    "user_key": "legacy-plaintext",
                    "enabled": True,
                }
            ),
        )
    )
    await db_session.commit()

    resolved = await rot._resolve_old_key(db_session)
    assert resolved == old_key
    new = Fernet(Fernet.generate_key())

    rotated, blanked = await rot._rotate_pihole_passwords(db_session, old, new)
    assert (rotated, blanked) == (1, 0)
    pushover_rows = await rot._rotate_pushover_settings(db_session, old, new)
    assert pushover_rows == 1
    await db_session.commit()

    pw = (
        await db_session.execute(text("SELECT api_password FROM pihole_instances"))
    ).scalar_one()
    assert new.decrypt(pw.encode()).decode() == "hunter2"

    po = json.loads(
        (
            await db_session.execute(
                text("SELECT value FROM site_settings WHERE key = 'pushover_settings'")
            )
        ).scalar_one()
    )
    assert new.decrypt(po["app_token"].encode()).decode() == "tok"
    # Legacy plaintext must come out encrypted under the new key, not dropped.
    assert new.decrypt(po["user_key"].encode()).decode() == "legacy-plaintext"
    assert po["enabled"] is True


async def test_rotation_blanks_undecryptable_password(db_session):
    """A password that doesn't decrypt under the old key is blanked —
    mirroring EncryptedString's read behaviour, where YAML re-sync
    restores the real value on next startup."""
    from sqlalchemy import text

    from app.models.site import Site

    rot = _load_rotation_module()

    site = Site(name="Rot2", slug="rot2", is_main=True)
    db_session.add(site)
    await db_session.flush()
    await db_session.execute(
        text(
            "INSERT INTO pihole_instances (id, site_id, name, url, api_password, color, is_active, is_master) "
            "VALUES (gen_random_uuid(), :sid, 'ph1', 'http://ph1', 'not-a-fernet-token', '#fff', true, true)"
        ),
        {"sid": site.id},
    )
    await db_session.commit()

    old = Fernet(Fernet.generate_key())
    new = Fernet(Fernet.generate_key())
    rotated, blanked = await rot._rotate_pihole_passwords(db_session, old, new)
    await db_session.commit()

    assert (rotated, blanked) == (0, 1)
    pw = (
        await db_session.execute(text("SELECT api_password FROM pihole_instances"))
    ).scalar_one()
    assert pw == ""
