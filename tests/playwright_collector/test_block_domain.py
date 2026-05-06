"""Block / unblock domain UI flow.

Drives the query-log modal end-to-end:

  1. Open /queries.
  2. Click the shield button on a row → opens the domain-status modal.
  3. The modal calls GET /api/domains/status/<domain>, which fans out
     to *every* configured Pi-hole and returns the union of deny/allow
     state. With the emulator's deny/allow lists empty, the modal
     should show "Not managed locally" and offer Block / Allow buttons.
  4. Click "Block" → POST /api/domains/deny → MyPi calls the emulator's
     POST /api/domains/deny/exact, which stores the domain in memory.
  5. Reopen the modal → status now shows "Blocked".
  6. Click "Remove Block" → DELETE /api/domains/deny/{domain} → emulator
     drops the domain from its in-memory list.

The tests exercise: the JS modal flow, the /api/domains/* MyPi routes,
the pihole_client deny/allow list calls, and the emulator's stateful
deny endpoints.
"""
from __future__ import annotations

import time

import httpx
from playwright.sync_api import Page, expect


def _wait_for_modal_status(page: Page, expected_text: str, timeout_ms: int = 8000) -> None:
    """Wait for the domain-status modal to render its status alert."""
    expect(page.locator("#domain-modal-body")).to_contain_text(
        expected_text, timeout=timeout_ms,
    )


def test_block_domain_via_query_modal(
    authed_page: Page, base_url: str, pihole_emulator: str, wait_for_first_poll,
):
    page = authed_page
    page.goto(f"{base_url}/queries")

    # Wait for at least one query row to render. The emulator queries
    # all use *.emu.test; the manage-button is rendered per row.
    expect(page.locator(".domain-manage-btn").first).to_be_visible(timeout=15_000)

    # Click the first query's shield button → opens the status modal.
    first_btn = page.locator(".domain-manage-btn").first
    target_domain = first_btn.get_attribute("data-domain")
    assert target_domain, "First query row had no data-domain attribute"
    first_btn.click()

    # Initial modal state — domain not in any list.
    _wait_for_modal_status(page, "Not in Deny List")

    # Click the "Block" button. Two paths render this with different
    # text ("Block" or "Block Instead"); both call doDomainAction('deny').
    page.locator(
        "#domain-modal-footer button:has-text('Block')"
    ).first.click()

    # The modal should swap to a success state (or close + toast).
    # MyPi's doDomainAction handler reloads the modal body on success,
    # so we wait for the "In Deny List" badge to appear.
    _wait_for_modal_status(page, "In Deny List", timeout_ms=10_000)

    # Cross-check: the emulator's GET /api/domains/deny/exact now
    # contains our domain. Hits the emulator directly so a regression
    # in the MyPi /api/domains/status round-trip can't mask a regression
    # in the actual block-write path.
    resp = httpx.get(
        f"{base_url}/api/domains/status/{target_domain}",
        cookies=page.context.cookies()
        and {c["name"]: c["value"] for c in page.context.cookies()},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["in_deny"] is True, f"Expected {target_domain} in deny list, got {body}"
    assert body["effective"] == "denied"


def test_unblock_domain_via_query_modal(
    authed_page: Page, base_url: str, pihole_emulator: str, wait_for_first_poll,
):
    """Pre-seed the emulator with a denied domain, then walk the
    "Remove Block" path through the UI."""
    # Seed: block ads.emu.test directly via the emulator. We don't go
    # through MyPi here because the previous test already covers the
    # block-write path; this test is specifically about removal.
    httpx.post(
        f"{pihole_emulator}/api/auth",
        json={"password": "emulator-password"}, timeout=5.0,
    ).raise_for_status()
    # The /__test__/reset autouse fixture wiped the list at test start;
    # we re-add via MyPi so the SID is shared with the running app's
    # pihole_client cache (and so the test exercises the same write
    # path the operator uses).
    cookies = {c["name"]: c["value"] for c in authed_page.context.cookies()}
    httpx.post(
        f"{base_url}/api/domains/deny",
        json={"domain": "ads.emu.test"},
        cookies=cookies, timeout=10.0,
    ).raise_for_status()

    page = authed_page
    page.goto(f"{base_url}/queries")

    # Find the row for our pre-blocked domain. The collector polls the
    # emulator queries on a 60s interval; we may need to wait a tick
    # for the row to render (or filter by domain to find it faster).
    page.fill("#f-domain", "ads.emu.test")
    page.keyboard.press("Enter")

    expect(page.locator(".domain-manage-btn").first).to_be_visible(timeout=15_000)
    page.locator(
        f".domain-manage-btn[data-domain='ads.emu.test']"
    ).first.click()

    # Modal should render the "Blocked" state with a Remove Block button.
    _wait_for_modal_status(page, "In Deny List")
    page.locator(
        "#domain-modal-footer button:has-text('Remove Block')"
    ).first.click()

    # After unblock, the modal refreshes to "Not in Deny List".
    _wait_for_modal_status(page, "Not in Deny List", timeout_ms=10_000)

    # Settle: emulator's list should be empty for ads.emu.test.
    # Poll briefly because the DELETE → next GET round-trip can lag.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        body = httpx.get(
            f"{base_url}/api/domains/status/ads.emu.test",
            cookies=cookies, timeout=5.0,
        ).json()
        if body.get("in_deny") is False:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"ads.emu.test still in deny list: {body}")
