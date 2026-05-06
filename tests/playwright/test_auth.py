"""Browser-driven tests for the login + forced-password-change flow."""
from __future__ import annotations

from playwright.sync_api import Page, expect

from .conftest import (
    ADMIN_BOOTSTRAP_PASSWORD,
    ADMIN_NEW_PASSWORD,
    ADMIN_USERNAME,
)


def test_login_page_renders(page: Page, base_url: str, db_reset):
    page.goto(f"{base_url}/login")
    expect(page).to_have_title("MyPi — Login")
    expect(page.locator("#username")).to_be_visible()
    expect(page.locator("#password")).to_be_visible()


def test_login_with_bad_password_shows_error(page: Page, base_url: str, db_reset):
    page.goto(f"{base_url}/login")
    page.fill("#username", ADMIN_USERNAME)
    page.fill("#password", "definitely-not-the-password")
    page.click("button[type=submit]")
    expect(page.locator(".alert-danger")).to_contain_text("Invalid username or password")


def test_unauthenticated_dashboard_redirects_to_login(page: Page, base_url: str, db_reset):
    page.goto(f"{base_url}/")
    expect(page).to_have_url(f"{base_url}/login")


def test_login_redirects_to_change_password_on_first_login(
    page: Page, base_url: str, db_reset,
):
    page.goto(f"{base_url}/login")
    page.fill("#username", ADMIN_USERNAME)
    page.fill("#password", ADMIN_BOOTSTRAP_PASSWORD)
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{base_url}/change-password")


def test_change_password_rejects_short_password(
    page: Page, base_url: str, db_reset,
):
    page.goto(f"{base_url}/login")
    page.fill("#username", ADMIN_USERNAME)
    page.fill("#password", ADMIN_BOOTSTRAP_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base_url}/change-password")

    page.fill("#current_password", ADMIN_BOOTSTRAP_PASSWORD)
    # Bypass the HTML5 minlength=8 attribute so the server-side check is
    # the one that catches us — that's the layer that actually matters.
    page.evaluate(
        "() => document.querySelectorAll('input[minlength]')"
        ".forEach(el => el.removeAttribute('minlength'))"
    )
    page.fill("#new_password", "short")
    page.fill("#confirm_password", "short")
    page.click("button[type=submit]")
    expect(page.locator(".alert-danger")).to_contain_text(
        "at least 8 characters",
    )


def test_change_password_rejects_mismatch(page: Page, base_url: str, db_reset):
    page.goto(f"{base_url}/login")
    page.fill("#username", ADMIN_USERNAME)
    page.fill("#password", ADMIN_BOOTSTRAP_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base_url}/change-password")

    page.fill("#current_password", ADMIN_BOOTSTRAP_PASSWORD)
    page.fill("#new_password", ADMIN_NEW_PASSWORD)
    page.fill("#confirm_password", ADMIN_NEW_PASSWORD + "-mismatch")
    page.click("button[type=submit]")
    expect(page.locator(".alert-danger")).to_contain_text("do not match")


def test_full_login_then_logout(logged_in_page: Page, base_url: str):
    # `logged_in_page` already walked login + change-password; landed on /.
    page = logged_in_page
    expect(page).to_have_url(f"{base_url}/")

    page.goto(f"{base_url}/logout")
    expect(page).to_have_url(f"{base_url}/login")

    # Cookie cleared — visiting / should redirect back to login.
    page.goto(f"{base_url}/")
    expect(page).to_have_url(f"{base_url}/login")
