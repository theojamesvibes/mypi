"""No page may violate its own Content-Security-Policy.

The strict style-src (no 'unsafe-inline') means any leftover style=""
attribute — server-rendered or injected via innerHTML — shows up as a
"Refused to apply inline style" console error in a real browser. The
header-level regression test lives in tests/integration/test_middleware.py;
this one proves the pages actually comply with the policy they ship.

Also spot-checks the CSSOM replacement path: per-instance colors moved
from style="background:…" (blocked) to data-bg + el.style.background
(allowed), so the seeded instance dot must still end up painted.
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def _collect_csp_violations(page: Page) -> list[str]:
    """Attach a console listener; returns a live list of CSP messages."""
    violations: list[str] = []

    def on_console(msg):
        if "Content Security Policy" in msg.text:
            violations.append(msg.text)

    page.on("console", on_console)
    return violations


def test_no_csp_violations_on_main_pages(
    authed_page: Page, base_url: str, seed_data,
):
    page = authed_page
    violations = _collect_csp_violations(page)

    for path in ("/", "/queries", "/settings", "/combined"):
        page.goto(f"{base_url}{path}")
        # Let the page's async renders (charts, tables, badges) land —
        # innerHTML-injected markup is where inline styles would resurface.
        page.wait_for_load_state("networkidle")

    assert violations == []


def test_no_csp_violations_on_login_page(page: Page, base_url: str):
    violations = _collect_csp_violations(page)
    page.goto(f"{base_url}/login")
    page.wait_for_load_state("networkidle")
    assert violations == []


def test_instance_dot_color_applied_via_cssom(
    authed_page: Page, base_url: str, seed_data,
):
    page = authed_page
    page.goto(f"{base_url}/")
    dot = page.locator("#instances-tbody .inst-dot").first
    expect(dot).to_be_visible(timeout=10_000)
    # data-bg is applied through el.style.background after render; a blocked
    # or missing application would leave the computed color transparent.
    bg = dot.evaluate("el => getComputedStyle(el).backgroundColor")
    assert bg not in ("", "rgba(0, 0, 0, 0)", "transparent"), bg
