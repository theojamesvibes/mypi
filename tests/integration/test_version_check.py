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
