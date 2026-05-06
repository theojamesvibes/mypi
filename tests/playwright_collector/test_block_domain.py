"""Block / unblock domain UI flow.

Drives the query-log modal end-to-end:

  1. Open /queries.
  2. Click the shield button on a row → opens the domain-status modal.
  3. The modal calls GET /api/domains/status/<domain>, which fans out
     to *every* configured Pi-hole and returns the union of deny/allow
     state. With the emulator's deny/allow lists empty, the modal
     should show "Not in Deny List" and offer Block / Allow buttons.
  4. Click "Block" → POST /api/domains/deny → MyPi calls the emulator's
     POST /api/domains/deny/exact, which stores the domain in memory.
  5. The modal swaps to a "Applied to N/M instance(s)" success alert.
     We *don't* re-fetch status inside the modal — the JS leaves the
     post-action state visible until the user clicks Done. To verify
     the state actually changed we cross-check via the MyPi
     /api/domains/status route on the same domain.

The tests exercise: the JS modal flow, the /api/domains/* MyPi routes,
the pihole_client deny/allow list calls, and the emulator's stateful
deny endpoints.
"""
from __future__ import annotations

import time

import httpx
from playwright.sync_api import Page, expect


def _wait_for_modal_text(page: Page, expected_text: str, timeout_ms: int = 8000) -> None:
    """Wait for the domain-status modal body to contain a substring."""
    expect(page.locator("#domain-modal-body")).to_contain_text(
        expected_text, timeout=timeout_ms,
    )


def _domain_status(base_url: str, page: Page, domain: str) -> dict:
    """Hit MyPi's /api/domains/status using the page's session cookies.

    Used as ground truth — the modal's post-action UI shows
    "Applied to N/M" rather than re-rendering the deny/allow badges,
    so we have to ask the API directly to confirm state changed.
    """
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    resp = httpx.get(
        f"{base_url}/api/domains/status/{domain}",
        cookies=cookies, timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()


def test_block_domain_via_query_modal(
    authed_page: Page, base_url: str, pihole_emulator: str, wait_for_first_poll,
):
    page = authed_page
    page.goto(f"{base_url}/queries")

    # Wait for at least one query row to render. The emulator queries
    # all use *.emu.test; the manage button is rendered per row.
    expect(page.locator(".domain-manage-btn").first).to_be_visible(timeout=15_000)

    # Click the first query's shield button → opens the status modal.
    first_btn = page.locator(".domain-manage-btn").first
    target_domain = first_btn.get_attribute("data-domain")
    assert target_domain, "First query row had no data-domain attribute"
    first_btn.click()

    # Initial modal state — domain not in any list.
    _wait_for_modal_text(page, "Not in Deny List")

    # Click the "Block" button. Two paths render this with different
    # text ("Block" or "Block Instead"); both call doDomainAction('deny').
    page.locator(
        "#domain-modal-footer button:has-text('Block')"
    ).first.click()

    # On success the modal body is replaced with an "Applied to N/M
    # instance(s)" alert and the footer collapses to a single Done
    # button. Both are stable post-action signals.
    _wait_for_modal_text(page, "Applied to 1/1 instance", timeout_ms=10_000)
    expect(page.locator("#domain-modal-footer button:has-text('Done')")).to_be_visible()

    # Cross-check that the deny list actually changed. /api/domains/status
    # round-trips through the pihole_client to the emulator's GET
    # /api/domains/deny/exact, so a regression anywhere in that chain
    # would show up here.
    body = _domain_status(base_url, page, target_domain)
    assert body["in_deny"] is True, f"Expected {target_domain} in deny list, got {body}"
    assert body["effective"] == "denied"


def test_unblock_domain_via_query_modal(
    authed_page: Page, base_url: str, pihole_emulator: str, wait_for_first_poll,
):
    """Pre-seed the deny list with a domain, then walk the
    Remove Block path through the UI."""
    page = authed_page

    # Seed the deny list via MyPi (rather than the emulator directly)
    # so the SID and client_manager state match what a real operator
    # action would produce.
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    seed_resp = httpx.post(
        f"{base_url}/api/domains/deny",
        json={"domain": "ads.emu.test"},
        cookies=cookies, timeout=10.0,
    )
    seed_resp.raise_for_status()

    page.goto(f"{base_url}/queries")
    page.fill("#f-domain", "ads.emu.test")
    page.keyboard.press("Enter")

    # Find the row for our pre-blocked domain. Filter narrows the
    # /api/queries fetch so the row is in the first page.
    target_btn = page.locator(
        ".domain-manage-btn[data-domain='ads.emu.test']"
    ).first
    expect(target_btn).to_be_visible(timeout=15_000)
    target_btn.click()

    # Modal should render the "Blocked" state with a Remove Block button.
    _wait_for_modal_text(page, "In Deny List")
    page.locator(
        "#domain-modal-footer button:has-text('Remove Block')"
    ).first.click()

    # Same post-action signal as the block test — Applied to 1/1 +
    # Done button.
    _wait_for_modal_text(page, "Applied to 1/1 instance", timeout_ms=10_000)
    expect(page.locator("#domain-modal-footer button:has-text('Done')")).to_be_visible()

    # Cross-check: deny list no longer contains the domain. Poll
    # briefly because client_manager's cached deny-list view can lag a
    # tick behind the DELETE.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        body = _domain_status(base_url, page, "ads.emu.test")
        if body.get("in_deny") is False:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"ads.emu.test still in deny list: {body}")
