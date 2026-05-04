"""Tests for app/services/pihole_version_check.py — the hourly poll
that fetches Pi-hole core/FTL/web release versions from GitHub and
flags `update_available_*` on each instance row."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    from app.services import pihole_version_check
    monkeypatch.setattr(pihole_version_check, "_enabled", True)
    monkeypatch.setattr(pihole_version_check, "_latest", {})
    monkeypatch.setattr(pihole_version_check, "_checked_at", None)
    monkeypatch.setattr(pihole_version_check, "_check_in_progress", False)
    yield


def test_compute_update_available_with_no_installed_returns_none():
    from app.services import pihole_version_check
    assert pihole_version_check.compute_update_available(None, "core") is None
    assert pihole_version_check.compute_update_available("", "core") is None


def test_compute_update_available_with_no_cached_returns_none():
    """`_latest` empty → can't compute, returns None so callers can
    distinguish 'not yet checked' from 'up to date'."""
    from app.services import pihole_version_check
    assert pihole_version_check.compute_update_available("6.4.2", "core") is None


def test_compute_update_available_compares_strings(monkeypatch):
    from app.services import pihole_version_check
    monkeypatch.setattr(pihole_version_check, "_latest", {
        "core": "6.4.2", "ftl": "6.6.1", "web": "6.5",
    })
    assert pihole_version_check.compute_update_available("6.4.2", "core") is False
    assert pihole_version_check.compute_update_available("6.4.1", "core") is True


async def test_check_now_fetches_three_repos_and_strips_v(respx_mock):
    """Iterates _REPOS and stores tags with leading 'v' stripped."""
    from app.services import pihole_version_check

    respx_mock.get(
        "https://api.github.com/repos/pi-hole/pi-hole/releases/latest"
    ).respond(200, json={"tag_name": "v6.4.2"})
    respx_mock.get(
        "https://api.github.com/repos/pi-hole/FTL/releases/latest"
    ).respond(200, json={"tag_name": "v6.6.1"})
    respx_mock.get(
        "https://api.github.com/repos/pi-hole/web/releases/latest"
    ).respond(200, json={"tag_name": "v6.5"})

    await pihole_version_check.check_now()

    status = pihole_version_check.get_status()
    assert status["latest_core"] == "6.4.2"
    assert status["latest_ftl"] == "6.6.1"
    assert status["latest_web"] == "6.5"
    assert status["checked_at"] is not None


async def test_check_now_disabled_skips_http(respx_mock):
    from app.services import pihole_version_check

    route = respx_mock.get(
        "https://api.github.com/repos/pi-hole/pi-hole/releases/latest"
    ).respond(200, json={"tag_name": "v6.4.2"})

    pihole_version_check._enabled = False
    await pihole_version_check.check_now()
    assert route.call_count == 0


async def test_check_now_preserves_last_known_on_transient_failure(respx_mock, monkeypatch):
    """A 500 on a single repo doesn't blank that component's cached
    value — operators don't lose 'update available' indicators when
    GitHub momentarily errors."""
    from app.services import pihole_version_check

    monkeypatch.setattr(pihole_version_check, "_latest", {
        "core": "6.4.2", "ftl": "6.6.1", "web": "6.5",
    })

    respx_mock.get(
        "https://api.github.com/repos/pi-hole/pi-hole/releases/latest"
    ).respond(500)
    respx_mock.get(
        "https://api.github.com/repos/pi-hole/FTL/releases/latest"
    ).respond(200, json={"tag_name": "v6.6.2"})
    respx_mock.get(
        "https://api.github.com/repos/pi-hole/web/releases/latest"
    ).respond(200, json={"tag_name": "v6.5"})

    await pihole_version_check.check_now()
    status = pihole_version_check.get_status()
    # core preserved
    assert status["latest_core"] == "6.4.2"
    # ftl + web updated
    assert status["latest_ftl"] == "6.6.2"
    assert status["latest_web"] == "6.5"
