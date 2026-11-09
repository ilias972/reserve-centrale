"""Pastille mobile testée avec de vraies demandes et un vrai navigateur."""

from pathlib import Path

# Réutiliser les helpers sans hériter des tests du module de recette précédent.
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.requests.models import InternalRequest
from tests.e2e import test_deliveries as delivery_browser
from tests.test_deliveries import DeliveryFixtures


class NavigationBadgeBrowserTests(DeliveryFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def page(self, browser, user, path):
        page = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            locale="fr-FR",
        ).new_page()
        self.login(page, user)
        page.goto(self.live_server_url + path)
        page.wait_for_load_state("networkidle")
        return page

    def test_badge_appears_updates_and_disappears_without_page_reload(self):
        product = self.demandable_product(code="BADGE-LIVE")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher, "/stocks")
            badge = page.locator("#pending-request-count")
            expect(badge).to_be_hidden()
            self.assertEqual(badge.text_content(), "")
            page.evaluate("window.badgePageMarker='same-document'")
            created = self.db(lambda: self.send([product], [2]).json()["resource"]["id"])
            expect(badge).to_be_visible(timeout=10000)
            expect(badge).to_have_text("1")
            expect(page.get_by_role("link", name="Demandes", exact=True)).to_be_visible()
            self.assertEqual(page.evaluate("window.badgePageMarker"), "same-document")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            folder = Path("docs/recette/COMPTEUR-DEMANDES/captures")
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / "une-demande.png"))
            self.db(lambda: self.send([product], [1]))
            expect(badge).to_have_text("2", timeout=10000)

            def cancel_first():
                return self.cancel_request(InternalRequest.objects.get(pk=created)).status_code

            self.assertEqual(self.db(cancel_first), 200)
            expect(badge).to_have_text("1", timeout=10000)

            def cancel_last():
                return self.cancel_request(InternalRequest.objects.get(status="NEW")).status_code

            self.assertEqual(self.db(cancel_last), 200)
            expect(badge).to_be_hidden(timeout=10000)
            expect(badge).to_have_text("")
            expect(page.locator("#pending-request-description")).to_have_text(
                "0 demandes à traiter"
            )
            self.assertEqual(page.evaluate("window.badgePageMarker"), "same-document")
            page.screenshot(path=str(folder / "aucune-demande.png"))
            browser.close()

    def test_badge_is_present_on_each_replenisher_screen_and_failure_keeps_last_count(self):
        request, _ = self.prepare()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher, "/mon-compte")
            for path in [
                "/stocks",
                "/demandes",
                "/mon-compte",
                "/stocks/mouvements",
                f"/demandes/{request.pk}",
            ]:
                page.goto(self.live_server_url + path)
                expect(page.locator("#pending-request-count")).to_have_text("1")
                expect(page.locator("#pending-request-count")).to_be_visible()
            page.route("**/api/v1/requests/pending-count", lambda route: route.abort("failed"))
            expect(page.locator("#live-status")).to_contain_text(
                "Actualisation indisponible", timeout=12000
            )
            expect(page.locator("#pending-request-count")).to_have_text("1")
            page.context.set_offline(True)
            expect(page.locator("#live-status")).to_contain_text("Hors connexion")
            expect(page.locator("#pending-request-count")).to_have_text("1")
            page.unroute("**/api/v1/requests/pending-count")
            self.db(lambda: self.deliver(request))
            page.context.set_offline(False)
            page.goto(self.live_server_url + "/stocks")
            expect(page.locator("#pending-request-count")).to_be_hidden()
            browser.close()
