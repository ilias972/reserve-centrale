"""Distinguer absence de remise et manque physique, sur une base fictive isolée."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from playwright.sync_api import expect, sync_playwright

from apps.inventory.models import StockEntry
from apps.requests.models import Delivery
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class DeliveryClarityBrowserTests(DeliveryFixtures, StaticLiveServerTestCase):
    def db(self, action):
        def run():
            close_old_connections()
            try:
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def login(self, page):
        page.goto(self.live_server_url + "/connexion")
        page.get_by_label("Identifiant", exact=True).fill(self.replenisher.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")

    def capture(self, page, name):
        folder = Path("docs/recette/REMISE-ZERO/captures")
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / name), full_page=True)

    def test_zero_handed_shortage_requires_explicit_physical_acknowledgment(self):
        request, products = self.prepare()
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.login(page)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            page.locator(".handed-quantity").fill("0")
            page.get_by_label("Le reste est réellement introuvable en réserve", exact=True).check()
            page.locator("#delivery-review").click()
            expect(page.locator("#delivery-consequence")).to_contain_text("4 unités introuvables")
            expect(page.locator("#delivery-consequence")).to_contain_text("simple refus")
            expect(page.locator("#delivery-confirm")).to_be_disabled()
            # Même un clic programmatique ne doit pas envoyer le formulaire sans l'attestation.
            page.locator("#delivery-confirm").evaluate("button=>button.click()")
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (10, 4, 6))
            self.capture(page, "zero-manque-attestation.png")
            page.locator("#shortage-ack").check()
            expect(page.locator("#delivery-confirm")).to_be_enabled()
            # Modifier les quantités impose une nouvelle vérification et une nouvelle attestation.
            page.locator("#delivery-back").click()
            page.locator(".handed-quantity").fill("1")
            page.locator("#delivery-review").click()
            expect(page.locator("#shortage-ack")).not_to_be_checked()
            expect(page.locator("#delivery-confirm")).to_be_disabled()
            page.locator("#shortage-ack").check()
            page.locator("#delivery-confirm").click()
            expect(page.locator("#request-status")).to_contain_text("clôturée pour manque")
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 0, 6))
            self.assertEqual(self.db(lambda: StockEntry.objects.get(kind="DELIVER").delta_p), -1)
            self.assertEqual(self.db(lambda: StockEntry.objects.get(kind="SHORTAGE").delta_p), -3)
            self.assertEqual(errors, [])
            browser.close()

    def test_zero_handed_later_cannot_create_a_false_delivery_or_stock_correction(self):
        request, products = self.prepare()
        before = self.db(lambda: StockEntry.objects.count())
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            self.login(page)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            page.locator(".handed-quantity").fill("0")
            page.get_by_label("Le reste sera apporté plus tard", exact=True).check()
            page.locator("#delivery-review").click()
            expect(page.locator("#operation-result")).to_contain_text("Aucune remise enregistrée")
            expect(page.locator("#delivery-recap")).to_be_hidden()
            expect(page.locator("#request-status")).to_have_text("En préparation")
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), before)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (10, 4, 6))
            browser.close()

    def test_replenisher_form_text_and_touch_policy_on_actual_pages(self):
        request, _ = self.prepare()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            self.login(page)
            for path in [
                "/mon-compte",
                "/stocks",
                "/stocks/inventaire-initial",
                "/demandes",
                f"/demandes/{request.pk}",
            ]:
                page.goto(self.live_server_url + path)
                page.wait_for_load_state("networkidle")
                self.assertEqual(
                    page.locator("body").evaluate("n=>getComputedStyle(n).touchAction"),
                    "pan-x pan-y",
                )
                sizes = page.locator("input:visible,select:visible,textarea:visible").evaluate_all(
                    "nodes=>nodes.map(n=>parseFloat(getComputedStyle(n).fontSize))"
                )
                self.assertTrue(all(size >= 16 for size in sizes), (path, sizes))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                if path.endswith(str(request.pk)):
                    page.locator(".handed-quantity").focus()
                    self.assertEqual(page.evaluate("visualViewport.scale"), 1)
                    self.capture(page, "remise-champs-lisibles.png")
            browser.close()

    def test_refuse_without_justification_and_cancel_review_leave_real_stock_unchanged(self):
        request, products = self.prepare((4, 3))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            self.login(page)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            page.get_by_role(
                "link", name="Ne rien remettre : refuser la demande", exact=True
            ).click()
            expect(page.get_by_label("Justification facultative")).not_to_have_attribute(
                "required", ""
            )
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            page.get_by_role("link", name="Conserver la demande", exact=True).click()
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]), [(10, 4, 6), (10, 3, 7)]
            )
            page.locator("#refuse-link").click()
            self.capture(page, "refuser-sans-manque.png")
            page.get_by_role("button", name="Confirmer le refus", exact=True).click()
            expect(page.locator("#request-status")).to_have_text("Refusée")
            expect(page.locator("#refuse-link")).to_have_count(0)
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]), [(10, 0, 10)] * 2
            )
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            self.capture(page, "refus-confirme.png")
            browser.close()

    def test_lost_refusal_response_double_click_and_reload_recover_one_release(self):
        request, products = self.prepare()
        from apps.requests.models import RequestEvent

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            self.login(page)
            page.goto(self.live_server_url + f"/demandes/{request.pk}/refuser")
            page.get_by_label("Justification facultative").fill("Texte fictif <b>conservé</b>")

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort("failed")

            page.route(f"**/api/v1/requests/{request.pk}/refuse", lose, times=1)
            page.locator("#refuse-confirm").evaluate("button=>{button.click();button.click();}")
            expect(page.locator("#operation-recovery")).to_be_visible()
            expect(page.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            self.assertEqual(
                self.db(lambda: RequestEvent.objects.filter(kind="REFUSED").count()), 1
            )
            page.reload()
            page.wait_for_url(f"**/demandes/{request.pk}")
            expect(page.locator("#request-status")).to_have_text("Refusée")
            expect(page.locator(".event-message")).to_have_text("Texte fictif <b>conservé</b>")
            expect(page.locator(".event-message b")).to_have_count(0)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (10, 0, 10))
            self.assertEqual(self.db(lambda: StockEntry.objects.filter(kind="RELEASE").count()), 1)
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            # Suivi demandeur — profil A sur un vrai second navigateur/session, sans données globales.
            assistant = browser.new_page(viewport={"width": 1440, "height": 900})
            assistant.goto(self.live_server_url + "/connexion")
            assistant.get_by_label("Identifiant", exact=True).fill(self.assistant.username)
            assistant.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
            assistant.get_by_role("button", name="Se connecter", exact=True).click()
            assistant.wait_for_url("**/mon-compte")
            assistant.goto(self.live_server_url + f"/demandes/{request.pk}")
            expect(assistant.locator("#request-status")).to_have_text("Refusée")
            expect(assistant.locator(".event-message")).to_have_text("Texte fictif <b>conservé</b>")
            expect(assistant.locator("#cancel-open")).to_have_count(0)
            self.capture(assistant, "demandeur — profil A-refus.png")
            browser.close()

    def test_refusal_hidden_and_server_form_unavailable_after_partial_delivery(self):
        request, _ = self.prepare()
        self.assertEqual(self.deliver(request, [1], "LATER").status_code, 201)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
            self.login(page)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            expect(page.locator("#refuse-link")).to_have_count(0)
            page.goto(self.live_server_url + f"/demandes/{request.pk}/refuser")
            expect(page.locator("#refuse-request")).to_have_count(0)
            expect(page.get_by_text("Après une remise partielle", exact=False)).to_be_visible()
            browser.close()
