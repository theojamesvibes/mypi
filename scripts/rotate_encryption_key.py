"""Rotate the Fernet key that protects Pi-hole passwords and Pushover creds.

Why you'd run this: when ENCRYPTION_KEY isn't configured, MyPi auto-generates
a key and stores it in the app_settings table — the same database (and every
backup of it) that holds the ciphertext. Encryption-at-rest only means
something once the key lives outside the data store. This script:

  1. resolves the current key (ENCRYPTION_KEY env, else app_settings),
  2. generates a fresh Fernet key,
  3. re-encrypts pihole_instances.api_password and the Pushover
     app_token/user_key fields under the new key (one transaction),
  4. deletes the app_settings copy of the old key,
  5. prints the new key for you to put in .env as ENCRYPTION_KEY.

Run it inside the app container so DATABASE_URL/SECRET_KEY are present:

    docker compose exec app python scripts/rotate_encryption_key.py

Then set ENCRYPTION_KEY in .env and restart. Treat database backups taken
before the rotation as if they contain plaintext passwords — they hold the
old key alongside the old ciphertext.

Values that fail to decrypt under the old key are handled the way the app
already does: api_password is blanked (config sync restores it from
pihole_instances.yml on next startup); Pushover fields are assumed to be
legacy plaintext and encrypted as-is under the new key.
"""
from __future__ import annotations

import asyncio
import json
import sys

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

_ENCRYPTION_KEY_SETTING = "encryption_key"
_PUSHOVER_SETTINGS_KEY = "pushover_settings"


async def _resolve_old_key(session) -> str | None:
    from app.config import settings

    if settings.encryption_key:
        return settings.encryption_key
    row = (
        await session.execute(
            text("SELECT value FROM app_settings WHERE key = :k"),
            {"k": _ENCRYPTION_KEY_SETTING},
        )
    ).first()
    return row[0] if row and row[0] else None


async def _rotate_pihole_passwords(session, old: Fernet, new: Fernet) -> tuple[int, int]:
    rotated = blanked = 0
    rows = (
        await session.execute(
            text("SELECT id, api_password FROM pihole_instances WHERE api_password <> ''")
        )
    ).all()
    for instance_id, ciphertext in rows:
        try:
            plaintext = old.decrypt(ciphertext.encode()).decode()
            new_value = new.encrypt(plaintext.encode()).decode()
            rotated += 1
        except InvalidToken:
            # Same semantics as EncryptedString.process_result_value: an
            # undecryptable row reads back as "" and config sync restores
            # the real password from pihole_instances.yml on next startup.
            print(f"  WARN instance {instance_id}: api_password undecryptable — blanking (YAML re-sync will restore it)")
            new_value = ""
            blanked += 1
        await session.execute(
            text("UPDATE pihole_instances SET api_password = :v WHERE id = :id"),
            {"v": new_value, "id": instance_id},
        )
    return rotated, blanked


def _reencrypt_field(value: str, old: Fernet, new: Fernet) -> str:
    if not value:
        return value
    try:
        plaintext = old.decrypt(value.encode()).decode()
    except InvalidToken:
        # Legacy plaintext (pre-encryption row) — same fallback the app's
        # pushover._decrypt uses.
        plaintext = value
    return new.encrypt(plaintext.encode()).decode()


async def _rotate_pushover_settings(session, old: Fernet, new: Fernet) -> int:
    rotated = 0
    rows = (
        await session.execute(
            text("SELECT site_id, value FROM site_settings WHERE key = :k"),
            {"k": _PUSHOVER_SETTINGS_KEY},
        )
    ).all()
    for site_id, raw in rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            print(f"  WARN site {site_id}: pushover_settings is not valid JSON — skipping")
            continue
        for field in ("app_token", "user_key"):
            data[field] = _reencrypt_field(data.get(field, ""), old, new)
        await session.execute(
            text("UPDATE site_settings SET value = :v WHERE site_id = :sid AND key = :k"),
            {"v": json.dumps(data), "sid": site_id, "k": _PUSHOVER_SETTINGS_KEY},
        )
        rotated += 1
    return rotated


async def main() -> int:
    # Imported here so a missing DATABASE_URL/SECRET_KEY fails with our
    # message below instead of a bare pydantic traceback at import time.
    try:
        from app.database import AsyncSessionLocal, engine
    except Exception as exc:
        print(f"Could not initialise app settings ({exc}).")
        print("Run inside the app container: docker compose exec app python scripts/rotate_encryption_key.py")
        return 1

    async with AsyncSessionLocal() as session:
        old_key = await _resolve_old_key(session)
        if old_key is None:
            print("No encryption key found (ENCRYPTION_KEY unset, no app_settings row).")
            print("Nothing to rotate — the app will generate a key on next startup.")
            return 1
        try:
            old = Fernet(old_key.encode())
        except Exception:
            print("Current encryption key is not a valid Fernet key — refusing to rotate.")
            return 1

        new_key = Fernet.generate_key().decode()
        new = Fernet(new_key.encode())

        rotated, blanked = await _rotate_pihole_passwords(session, old, new)
        pushover = await _rotate_pushover_settings(session, old, new)
        await session.execute(
            text("DELETE FROM app_settings WHERE key = :k"),
            {"k": _ENCRYPTION_KEY_SETTING},
        )
        await session.commit()
    await engine.dispose()

    print()
    print(f"Rotated {rotated} Pi-hole password(s) ({blanked} blanked), {pushover} Pushover settings row(s).")
    print("Old key removed from app_settings.")
    print()
    print("Add this to your .env, then restart MyPi:")
    print()
    print(f"    ENCRYPTION_KEY={new_key}")
    print()
    print("Until the restart, the running app still holds the old key and will")
    print("fail to decrypt the re-encrypted values — restart promptly.")
    print("Treat pre-rotation database backups as containing plaintext passwords:")
    print("they include the old key next to the old ciphertext.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
