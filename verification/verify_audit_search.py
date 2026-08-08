import os
import time

from playwright.sync_api import expect, sync_playwright


def test_audit_logs_search():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Step 1: Go to login page
        print("Navigating to login...")
        page.goto("http://localhost:8000/login/")
        page.wait_for_load_state("networkidle")

        # Step 2: Login
        print("Logging in...")
        page.fill("#id_username", "admin")
        page.fill("#id_password", "AdminPassword123!")

        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")

        print("Current URL:", page.url)
        # If redirected to password change, let's handle it or change password
        if "password-change" in page.url or "password_change" in page.url:
            print("Redirected to password change. Creating strong credentials...")  # nosec # NOSONAR # nosemgrep
            page.fill("#id_old_password", "AdminPassword123!")
            page.fill("#id_new_password1", "StrongerAdminPass123!_@")
            page.fill("#id_new_password2", "StrongerAdminPass123!_@")
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")
            print("Password changed successfully, URL:", page.url)  # nosec # NOSONAR # nosemgrep

        # Step 3: Go to Audit Logs page
        print("Navigating to Audit Logs...")
        page.goto("http://localhost:8000/audit-logs/")
        page.wait_for_load_state("networkidle")

        # Step 4: Verify initial state of Audit Logs filter search
        print("Verifying filter search input...")
        search_input = page.locator("#search-input")
        expect(search_input).to_be_visible()

        shortcut_hint = page.locator("#audit-search-hint")
        expect(shortcut_hint).to_be_visible()

        # Take initial screenshot of the Audit Logs page
        os.makedirs("verification", exist_ok=True)
        page.screenshot(path="verification/01_initial_audit_logs.png")
        print("Saved verification/01_initial_audit_logs.png")

        # Step 5: Press '/' key to focus the search input
        print("Pressing '/' key to focus...")
        page.press("body", "/")
        time.sleep(0.5)

        # Check if focused and if shortcut hint fades/hides
        expect(shortcut_hint).to_be_hidden()

        # Type search query
        print("Typing search query...")
        page.keyboard.type("Login successful")
        time.sleep(0.5)

        # Take screenshot of active text search
        page.screenshot(path="verification/02_active_audit_search.png")
        print("Saved verification/02_active_audit_search.png")

        # Step 6: Click clear button
        print("Clicking clear button...")
        clear_btn = page.locator(".search-clear-btn")
        expect(clear_btn).to_be_visible()
        clear_btn.click()
        time.sleep(0.5)

        # Verify search input is cleared
        expect(search_input).to_have_value("")

        # Take final screenshot confirming clean search state
        page.screenshot(path="verification/03_cleared_audit_search.png")
        print("Saved verification/03_cleared_audit_search.png")

        browser.close()


if __name__ == "__main__":
    test_audit_logs_search()
