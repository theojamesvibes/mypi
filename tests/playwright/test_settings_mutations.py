"""Settings page mutations: sync schedule, API key create/revoke,
session timeout, poll interval.

These exercise routes that ship in production but only had integration
coverage before — never a real form-submission round-trip from a
browser. They're meant to catch JS regressions where the form posts
the wrong shape, fails silently, or doesn't reflect the new state on
the next page load.

Tests use the cached `authed_page` fixture (no per-test login round-
trip) and the seeded-DB suite's setup, so they run alongside the
existing settings smoke test in the same CI job.
"""
from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect


# ── Sync schedule ──────────────────────────────────────────────────────────


def test_sync_schedule_save_persists(
    authed_page: Page, base_url: str, db_reset_data,
):
    """Pick a non-default interval + flip auto_gravity, click Save,
    reload the page, assert the new values came back.

    Sync schedule is the canonical "this setting must round-trip
    through site_settings" test — anything that breaks the
    site_settings write path (e.g. the data-migration in 0015)
    will surface here as a missing value on reload.
    """
    page = authed_page
    page.goto(f"{base_url}/settings")

    # Wait for the schedule form to populate with current values.
    expect(page.locator("#sync-interval")).to_be_visible(timeout=10_000)

    # Choose 60-minute interval. The select has options at fixed values;
    # 60 is one that ships out of the box.
    page.select_option("#sync-interval", "60")
    page.check("#sync-auto-gravity")

    page.locator("button[onclick='saveSchedule()']").click()

    # The save handler updates the button label briefly. Reload the
    # page and verify the values stuck — that's the real assertion.
    page.reload()
    expect(page.locator("#sync-interval")).to_have_value("60", timeout=10_000)
    expect(page.locator("#sync-auto-gravity")).to_be_checked()


# ── Session timeout ────────────────────────────────────────────────────────


def test_session_timeout_save_persists(
    authed_page: Page, base_url: str, db_reset_data,
):
    """Picking a session timeout writes it to app_settings and the
    next page load reflects the choice."""
    page = authed_page
    page.goto(f"{base_url}/settings")
    expect(page.locator("#session-timeout")).to_be_visible(timeout=10_000)

    page.select_option("#session-timeout", "240")  # 4 hours
    page.locator("button[onclick='saveSessionTimeout()']").click()

    # The save handler shows a confirmation; reload to verify persistence.
    page.reload()
    expect(page.locator("#session-timeout")).to_have_value("240", timeout=10_000)


# ── API key create / revoke ────────────────────────────────────────────────


def test_api_key_create_displays_raw_key_once(
    authed_page: Page, base_url: str, db_reset_data,
):
    """The create-key form is the *only* moment the raw key is visible
    to the user — the alert that holds it must render exactly once,
    with the key shown in full. Subsequent loads only show the metadata
    (name, created, last used)."""
    page = authed_page
    page.goto(f"{base_url}/settings")

    expect(page.locator("#key-name")).to_be_visible(timeout=10_000)
    page.fill("#key-name", "Test-iPhone")
    page.locator("#create-key-form button[type=submit]").click()

    # The alert appears with the raw key inside <code id="new-key-value">.
    new_key_box = page.locator("#new-key-value")
    expect(new_key_box).to_be_visible(timeout=10_000)

    raw_key = new_key_box.text_content() or ""
    raw_key = raw_key.strip()
    # MyPi keys are URL-safe base64 of HMAC-SHA256, so >= 40 chars.
    assert len(raw_key) >= 40, f"Raw key looks truncated: {raw_key!r}"

    # The key row should now be in the table by name.
    expect(page.locator("#api-keys-tbody")).to_contain_text("Test-iPhone")

    # The raw key actually authenticates against /api/auth/me — proves
    # we got the key the server stored, not a placeholder.
    me = httpx.get(
        f"{base_url}/api/auth/me",
        headers={"X-API-Key": raw_key},
        timeout=5.0,
    )
    assert me.status_code == 200, me.text
    assert me.json().get("username") in {"admin", "alice"}


def test_api_key_revoke_removes_from_list(
    authed_page: Page, base_url: str, db_reset_data,
):
    """After revoke, the key disappears from the table and stops
    authenticating against /api/auth/me."""
    page = authed_page
    page.goto(f"{base_url}/settings")

    # Auto-confirm the revoke `confirm()` dialog.
    page.on("dialog", lambda d: d.accept())

    # Create a key first so we have something to revoke.
    page.fill("#key-name", "Throwaway")
    page.locator("#create-key-form button[type=submit]").click()
    raw_key = (page.locator("#new-key-value").text_content() or "").strip()
    expect(page.locator("#api-keys-tbody")).to_contain_text("Throwaway", timeout=10_000)

    # Sanity: the key works before revocation.
    pre = httpx.get(
        f"{base_url}/api/auth/me",
        headers={"X-API-Key": raw_key},
        timeout=5.0,
    )
    assert pre.status_code == 200

    # Click Revoke on the row we just created.
    revoke_btn = page.locator(
        "#api-keys-tbody tr",
        has_text="Throwaway",
    ).locator("button:has-text('Revoke')")
    revoke_btn.click()

    # Row should disappear after the revoke + refetch.
    expect(page.locator("#api-keys-tbody")).not_to_contain_text(
        "Throwaway", timeout=10_000,
    )

    # The revoked key no longer authenticates.
    post = httpx.get(
        f"{base_url}/api/auth/me",
        headers={"X-API-Key": raw_key},
        timeout=5.0,
    )
    assert post.status_code == 401


# ── Poll interval (separate route from sync schedule) ─────────────────────


def test_poll_interval_save_persists(
    authed_page: Page, base_url: str, db_reset_data,
):
    """Query poll interval lives in site_settings under Main and is
    rescheduled in-process when saved. Verify the form save round-trips."""
    page = authed_page
    page.goto(f"{base_url}/settings")
    expect(page.locator("#poll-interval")).to_be_visible(timeout=10_000)

    page.select_option("#poll-interval", "30")
    page.locator("button[onclick='savePollInterval()']").click()

    # Wait briefly for the save handler to update the result line.
    expect(page.locator("#poll-interval-result")).to_contain_text(
        re.compile(r"saved|updated|✓|30", re.IGNORECASE),
        timeout=10_000,
    )

    page.reload()
    expect(page.locator("#poll-interval")).to_have_value("30", timeout=10_000)
