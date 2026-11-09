"""Real-browser language selection and Arabic direction smoke test."""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import expect, sync_playwright

from tests.test_accounts import FICTIVE_PASSWORD, AccountFixtures


@override_settings(LANGUAGE_CODE="en")
class LanguageSelectionBrowserTests(AccountFixtures, StaticLiveServerTestCase):
    def test_authenticated_user_can_switch_language_across_pages(self):
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.live_server_url + "/connexion")
            page.wait_for_load_state("networkidle")
            page.get_by_label("Username", exact=True).fill(self.assistant.username)
            page.get_by_label("Password", exact=True).fill(FICTIVE_PASSWORD)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.wait_for_url("**/mon-compte")
            expect(page.locator("html")).to_have_attribute("lang", "en")

            for code, direction in [("fr", "ltr"), ("ar", "rtl"), ("en", "ltr")]:
                page.locator("#language-choice").select_option(code)
                page.locator(".language-switcher button").click()
                page.wait_for_load_state("networkidle")
                expect(page.locator("html")).to_have_attribute("lang", code)
                expect(page.locator("html")).to_have_attribute("dir", direction)
                page.goto(self.live_server_url + "/catalogue")
                page.wait_for_load_state("networkidle")
                expect(page.locator("html")).to_have_attribute("lang", code)
                expect(page.locator("html")).to_have_attribute("dir", direction)

            browser.close()
        self.assertEqual(errors, [])

    def test_reference_screens_render_translated_titles_in_all_languages(self):
        errors = []
        expected = {
            "en": ("ltr", "/configuration/floor", "Units"),
            "fr": ("ltr", "/configuration/category", "Catégories"),
            "ar": ("rtl", "/configuration/supplier", "الموردون"),
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.live_server_url + "/connexion")
            page.get_by_label("Username", exact=True).fill(self.admin.username)
            page.get_by_label("Password", exact=True).fill(FICTIVE_PASSWORD)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.wait_for_load_state("networkidle")

            for language, (direction, path, heading) in expected.items():
                if page.locator("#language-choice").input_value() != language:
                    page.locator(".language-switcher:visible select").first.select_option(language)
                    page.locator(".language-switcher:visible button").first.click()
                    page.wait_for_load_state("networkidle")
                for width, height in [(320, 700), (390, 844), (1440, 900)]:
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(self.live_server_url + path)
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("html")).to_have_attribute("lang", language)
                    expect(page.locator("html")).to_have_attribute("dir", direction)
                    expect(page.locator("main h1")).to_have_text(heading)
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )

            browser.close()
        self.assertEqual(errors, [])

    def test_guest_can_switch_and_keep_english_french_and_arabic(self):
        page_errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(self.live_server_url + "/connexion")
            page.wait_for_load_state("networkidle")
            expect(page.locator("html")).to_have_attribute("lang", "en")
            expect(page.locator("html")).to_have_attribute("dir", "ltr")
            expect(page.locator("#language-choice")).to_have_value("en")
            self.assertEqual(page.evaluate("typeof window.gettext"), "function")
            self.assertEqual(page.evaluate("typeof window.ReserveI18n.plural"), "function")

            for code, direction, heading in [
                ("fr", "ltr", "Se connecter"),
                ("ar", "rtl", "تسجيل الدخول"),
                ("en", "ltr", "Sign in"),
            ]:
                page.locator("#language-choice").select_option(code)
                page.locator(".language-switcher button").click()
                expect(page.locator("html")).to_have_attribute("lang", code)
                expect(page.locator("html")).to_have_attribute("dir", direction)
                expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
                expect(page.locator("#language-choice")).to_have_value(code)
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"),
                    page.evaluate("window.innerWidth"),
                )

            browser.close()
        self.assertEqual(page_errors, [])
