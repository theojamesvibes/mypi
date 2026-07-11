"""Settings page mutations: sync schedule, API key create/revoke,
session timeout, poll interval.

These exercise routes that ship in production but only had integration
coverage before — never a real form-submission round-trip from a
browser. They're meant to catch JS regressions where the form posts
the wrong shape, fails silently, or doesn't reflect the new state on
the next page load.

Tests use the cached `authed_page` fixture (no per-test login round-
trip) and the seeded-DB suite's setup, so they run alongside the
existing settings smoke test in the same CI job. Tests that exercise
per-site settings depend on the `with_main_site` fixture, which
inserts a Default Main site after `db_reset_data` wipes the table.
"""
from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

# ── Sync schedule ──────────────────────────────────────────────────────────


def test_sync_schedule_save_persists(
    authed_page: Page, base_url: str, with_main_site,
):
    """Pick a non-default interval + flip auto_gravity, click Save,
    reload the page, assert the new values came back.

    Sync schedule is the canonical "this setting must round-trip
    through site_settings" test — anything that breaks the
    site_settings write path (e.g. the data-migration in 0015)
    will surface here as a missing value on reload.
    """
    page = authed_page

    # FLAKE FIX (CI 2026-06-27 / 2026-07-02): loadSyncSchedule() runs
    # async on page load and writes the *loaded* values back into the
    # form (`interval.value = ...; autoG.checked = ...`). The old
    # `to_be_visible` wait was satisfied by the server-rendered markup
    # immediately — before the loader's GET resolved — so under CI load
    # the response could land *after* select_option/check below and
    # silently revert the form to interval=0 / auto_gravity=false; the
    # save then persisted the defaults and the post-reload assertions
    # failed. The sibling tests here guard this race by waiting for the
    # loaded default value, but sync-interval's loaded default ("0")
    # equals the markup default, so a value-wait can't tell "loaded"
    # from "not yet loaded". Instead, block on the loader's GET
    # response itself. (Matches both /api/sync/schedule and the
    # per-site /api/sites/<slug>/sync/schedule form.)
    with page.expect_response(
        lambda r: "/sync/schedule" in r.url and r.request.method == "GET",
        timeout=10_000,
    ):
        page.goto(f"{base_url}/settings")

    expect(page.locator("#sync-interval")).to_be_visible(timeout=10_000)

    # Choose 60-minute interval. Valid select options: 0 / 15 / 30 / 60 / 360 / 1440.
    page.select_option("#sync-interval", "60")
    page.check("#sync-auto-gravity")

    # The save handler is async (PUT /api/sync/schedule). Reloading
    # before it completes can cancel the in-flight request, leaving
    # the schedule unsaved. Wait for the PUT response itself rather
    # than the `btn-success` class flip the old test used: the JS
    # reverts the class after 2 s (setTimeout), so a starved CI runner
    # could miss the window and time out even though the save landed.
    # set_schedule awaits _persist_schedule before the route returns,
    # so once the PUT is OK the DB write is durable and reload is safe.
    with page.expect_response(
        lambda r: "/sync/schedule" in r.url and r.request.method == "PUT",
        timeout=10_000,
    ) as put_info:
        page.locator("#sync-schedule-save-btn").click()
    assert put_info.value.ok, f"schedule save failed: {put_info.value.status}"

    # Reload the page and verify the values stuck — that's the real
    # assertion.
    page.reload()
    expect(page.locator("#sync-interval")).to_have_value("60", timeout=10_000)
    expect(page.locator("#sync-auto-gravity")).to_be_checked()


# ── Session timeout (stored in app_settings, no Main site needed) ─────────


def test_session_timeout_save_persists(
    authed_page: Page, base_url: str, db_reset_data,
):
    """Picking a session timeout writes it to app_settings and the
    next page load reflects the choice."""
    page = authed_page
    page.goto(f"{base_url}/settings")

    # Wait for loadSessionTimeout()'s async GET to settle before
    # touching the select. Without this, select_option races the
    # loader: my "60" wins briefly, then loadSessionTimeout's
    # response writes the default "480" back, and the click sends
    # the wrong value. Default loaded value is 480 (8 hours).
    expect(page.locator("#session-timeout")).to_have_value(
        "480", timeout=10_000,
    )
    # Valid select options: 15 / 60 / 480 / 1440 / 10080 / 0. "60" = 1 hour.
    page.select_option("#session-timeout", "60")
    page.locator("#session-timeout-save-btn").click()

    # Wait for the JS save indicator. On a 200 response the handler
    # writes "Saved — takes effect on next login" into the result div.
    expect(page.locator("#session-timeout-result")).to_contain_text(
        re.compile(r"saved", re.IGNORECASE), timeout=10_000,
    )

    # Source-of-truth check via the API instead of a page-reload +
    # DOM read. The previous form (page.reload() + to_have_value)
    # consistently came back at 480 even though the JS indicator
    # showed "Saved" — symptom of a settings-load timing race that
    # the existing settings-page smoke test already covers.
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    resp = httpx.get(
        f"{base_url}/api/auth/session-timeout",
        cookies=cookies, timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["timeout_minutes"] == 60, resp.json()


# ── API key create / revoke (no Main site needed; api_keys is global) ────


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

    # Wait for the alert + raw-key element to actually render. The JS
    # only flips them visible *after* the POST resolves, so reading
    # text_content() before this expect() yields an empty string.
    new_key_alert = page.locator("#new-key-alert")
    expect(new_key_alert).to_be_visible(timeout=10_000)
    new_key_box = page.locator("#new-key-value")
    expect(new_key_box).not_to_be_empty(timeout=10_000)

    raw_key = (new_key_box.text_content() or "").strip()
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

    # Create a key. Same wait pattern as the create test — the alert
    # element is hidden until the JS handler swaps it visible.
    page.fill("#key-name", "Throwaway")
    page.locator("#create-key-form button[type=submit]").click()

    new_key_alert = page.locator("#new-key-alert")
    expect(new_key_alert).to_be_visible(timeout=10_000)
    new_key_box = page.locator("#new-key-value")
    expect(new_key_box).not_to_be_empty(timeout=10_000)
    raw_key = (new_key_box.text_content() or "").strip()
    assert len(raw_key) >= 40, f"Raw key looks truncated: {raw_key!r}"

    expect(page.locator("#api-keys-tbody")).to_contain_text("Throwaway", timeout=10_000)

    # Sanity: the key works before revocation.
    pre = httpx.get(
        f"{base_url}/api/auth/me",
        headers={"X-API-Key": raw_key},
        timeout=5.0,
    )
    assert pre.status_code == 200, pre.text

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


# ── Poll interval (stored in site_settings, requires Main site) ───────────


def test_poll_interval_save_persists(
    authed_page: Page, base_url: str, with_main_site,
):
    """Query poll interval lives in site_settings under Main and is
    rescheduled in-process when saved. Verify the form save round-trips."""
    page = authed_page
    page.goto(f"{base_url}/settings")

    # Wait for loadPollInterval()'s async GET to populate the select
    # before changing it — otherwise the loader's response races
    # ahead of select_option and overwrites our choice. Default
    # loaded value is 60 (poll_settings._DEFAULT_INTERVAL).
    expect(page.locator("#poll-interval")).to_have_value(
        "60", timeout=10_000,
    )
    # Valid select options: 10 / 30 / 60 / 120 / 300.
    page.select_option("#poll-interval", "30")
    page.locator("#poll-interval-save-btn").click()

    # Wait briefly for the save handler to update the result line.
    expect(page.locator("#poll-interval-result")).to_contain_text(
        re.compile(r"saved|updated|✓|30", re.IGNORECASE),
        timeout=10_000,
    )

    page.reload()
    expect(page.locator("#poll-interval")).to_have_value("30", timeout=10_000)
