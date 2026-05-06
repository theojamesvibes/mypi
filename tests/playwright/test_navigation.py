"""Smoke tests for the major authenticated pages.

These don't seed any data — they just confirm each page renders its
shell and the user can navigate between them without bouncing back to
the login screen. Data-rendering assertions live in test_dashboard.py.
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_dashboard_shell_renders(logged_in_page: Page, base_url: str):
    page = logged_in_page
    expect(page).to_have_title("MyPi — Dashboard")
    expect(page.locator("h5", has_text="Dashboard").first).to_be_visible()
    expect(page.locator("#card-total")).to_be_visible()
    expect(page.locator("#card-blocked")).to_be_visible()
    expect(page.locator("#card-pct")).to_be_visible()
    expect(page.locator("#card-blocklist")).to_be_visible()


def test_queries_page_renders(logged_in_page: Page, base_url: str):
    page = logged_in_page
    page.goto(f"{base_url}/queries")
    expect(page).to_have_url(f"{base_url}/queries")
    expect(page).to_have_title("MyPi — Query Log")
    expect(page.locator("#f-domain")).to_be_visible()
    expect(page.locator("#f-client")).to_be_visible()


def test_settings_page_renders(logged_in_page: Page, base_url: str):
    page = logged_in_page
    page.goto(f"{base_url}/settings")
    expect(page).to_have_url(f"{base_url}/settings")
    expect(page).to_have_title("MyPi — Settings")
    expect(page.locator("#theme-btn-light")).to_be_visible()
    expect(page.locator("#theme-btn-dark")).to_be_visible()


def test_docs_page_renders(logged_in_page: Page, base_url: str):
    """ENABLE_API_DOCS=true is set by the live_app fixture so /docs is on."""
    page = logged_in_page
    page.goto(f"{base_url}/docs")
    expect(page).to_have_url(f"{base_url}/docs")
    # Swagger UI mounts a #swagger-ui div even before its JS finishes.
    expect(page.locator("#swagger-ui")).to_be_visible()


def test_health_endpoint_anonymous(page: Page, base_url: str):
    """/api/health is intentionally unauthenticated — used by the
    Docker HEALTHCHECK and by the live_app fixture's readiness probe."""
    page.goto(f"{base_url}/api/health")
    expect(page.locator("body")).to_contain_text('"status"')
