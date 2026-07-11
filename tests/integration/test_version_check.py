"""Tests for app/services/version_check.py — the hourly GitHub poll
that surfaces "update available" in the UI."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    from app.services import version_check
    monkeypatch.setattr(version_check, "_enabled", True)
    monkeypatch.setattr(version_check, "_current_version", "")
    monkeypatch.setattr(version_check, "_latest_version", "")
    monkeypatch.setattr(version_check, "_checked_at", None)
    monkeypatch.setattr(version_check, "_check_in_progress", False)
    yield


async def test_initialize_records_running_version():
    from app.services import version_check
    version_check.initialize("2.0.5")
    status = version_check.get_status()
    assert status["current_version"] == "2.0.5"


async def test_check_now_fetches_latest_from_github(respx_mock):
    """check_now hits the raw VERSION file on GitHub and stores the
    string in module state."""
    from app.services import version_check

    respx_mock.get(
        "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
    ).respond(200, text="2.1.0\n")

    version_check.initialize("2.0.5")
    await version_check.check_now()

    status = version_check.get_status()
    assert status["latest_version"] == "2.1.0"
    assert status["up_to_date"] is False
    assert status["checked_at"] is not None


async def test_check_now_marks_up_to_date_when_versions_match(respx_mock):
    from app.services import version_check

    respx_mock.get(
        "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
    ).respond(200, text="2.0.5\n")

    version_check.initialize("2.0.5")
    await version_check.check_now()

    assert version_check.get_status()["up_to_date"] is True


async def test_check_now_skipped_when_disabled(respx_mock):
    from app.services import version_check

    route = respx_mock.get(
        "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
    ).respond(200, text="2.1.0\n")

    version_check._enabled = False
    version_check.initialize("2.0.5")
    await version_check.check_now()

    # No HTTP request was made — the disabled flag short-circuits.
    assert route.call_count == 0


async def test_check_now_handles_github_failure_silently(respx_mock):
    """A 500 from raw.githubusercontent doesn't crash MyPi — the
    failure is logged and the next hourly tick will retry."""
    from app.services import version_check

    respx_mock.get(
        "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
    ).respond(500)

    version_check.initialize("2.0.5")
    # Must not raise.
    await version_check.check_now()
    # latest_version stays empty since the call didn't succeed.
    assert version_check.get_status()["latest_version"] == ""


async def test_check_now_skipped_when_already_in_progress(respx_mock):
    """The re-entrancy guard: an hourly tick that fires while a manual
    check is still running must not stack a second HTTP call."""
    from app.services import version_check

    route = respx_mock.get(
        "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
    ).respond(200, text="2.1.0\n")

    version_check._check_in_progress = True
    await version_check.check_now()

    assert route.call_count == 0
    assert version_check.get_status()["latest_version"] == ""


async def test_get_status_tolerates_unparseable_version():
    """A garbage version string parses to (0,) instead of raising, so
    the status endpoint keeps working."""
    from app.services import version_check

    version_check.initialize("not-a-version")
    version_check._latest_version = "2.1.0"

    assert version_check.get_status()["up_to_date"] is False


# ── load_settings — restores persisted state across restarts ────────────────


async def test_load_settings_restores_persisted_state():
    import json

    from app.database import AsyncSessionLocal
    from app.models.settings import AppSetting
    from app.services import version_check

    async with AsyncSessionLocal() as db:
        db.add(AppSetting(key="version_check", value=json.dumps({
            "enabled": False,
            "latest_version": "2.9.9",
            "checked_at": "2026-07-01T00:00:00+00:00",
        })))
        await db.commit()

    await version_check.load_settings()

    status = version_check.get_status()
    assert status["enabled"] is False
    assert status["latest_version"] == "2.9.9"
    assert status["checked_at"] == "2026-07-01T00:00:00+00:00"


async def test_load_settings_without_row_keeps_defaults():
    from app.services import version_check

    await version_check.load_settings()

    status = version_check.get_status()
    assert status["enabled"] is True
    assert status["latest_version"] == ""
    assert status["checked_at"] is None


async def test_load_settings_swallows_malformed_json():
    """A corrupt settings row must not take down startup — the check
    just runs with defaults until the next successful persist."""
    from app.database import AsyncSessionLocal
    from app.models.settings import AppSetting
    from app.services import version_check

    async with AsyncSessionLocal() as db:
        db.add(AppSetting(key="version_check", value="{not json"))
        await db.commit()

    # Must not raise.
    await version_check.load_settings()
    assert version_check.get_status()["enabled"] is True
