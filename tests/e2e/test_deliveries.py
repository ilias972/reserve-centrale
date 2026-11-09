"""Parcours M5 vrais HTTP/PostgreSQL : PC demandeur — profil A et téléphone émulé."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from playwright.sync_api import expect, sync_playwright

from apps.inventory.models import StockEntry
from apps.requests.models import Delivery, DeliveryIssue, InternalRequest
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class BrowserDeliveryTests(DeliveryFixtures, StaticLiveServerTestCase):
    def db(self, action):
        def run():
            close_old_connections()
            try:
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def login(self, page, user):
        page.goto(self.live_server_url + "/connexion")
        page.wait_for_load_state("networkidle")
        page.get_by_label("Identifiant", exact=True).fill(user.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")

    def phone(self, browser):
        return browser.new_context(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, locale="fr-FR"
        ).new_page()

    def evidence(self):
        folder = Path("reports/m5-browser")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def review(self, page):
        page.get_by_role("button", name="Vérifier la remise", exact=True).click()
        expect(page.get_by_role("heading", name="Vérifier avant confirmation")).to_be_visible()

    def test_real_assistant_to_phone_complete_three_products_and_shared_stock(self):
        products = [self.demandable_product(code=f"DEMO-NAV-{i}") for i in range(3)]
        folder = self.evidence()
        errors = []
        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            pc = browser.new_page(viewport={"width": 1440, "height": 1000})
            phone = self.phone(browser)
            for page in [pc, phone]:
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on(
                    "response", lambda r: failures.append(r.status) if r.status >= 500 else None
                )
            self.login(pc, self.assistant)
            pc.get_by_role("link", name="Faire une demande", exact=True).click()
            pc.wait_for_load_state("networkidle")
            for product in products:
                pc.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("2")
            pc.get_by_role("button", name="Vérifier ma demande", exact=True).click()
            pc.get_by_role("button", name="Envoyer la demande", exact=True).click()
            expect(pc.get_by_role("link", name="Suivre cette demande", exact=True)).to_be_visible()
            pc.get_by_role("link", name="Suivre cette demande", exact=True).click()
            expect(pc.locator("#request-status")).to_have_text("Nouvelle")
            identifier = self.db(lambda: str(InternalRequest.objects.get().pk))
            self.login(phone, self.replenisher)
            phone.get_by_role("link", name="Demandes", exact=True).click()
            phone.get_by_role(
                "link", name=self.db(lambda: InternalRequest.objects.get().reference), exact=True
            ).click()
            phone.get_by_role("button", name="Prendre en charge", exact=True).click()
            expect(phone.locator("#request-status")).to_have_text("En préparation")
            self.review(phone)
            expect(phone.locator("#delivery-summary")).to_contain_text("2 carton remis")
            phone.screenshot(path=str(folder / "recap-mobile.png"), full_page=True)
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            phone.get_by_role("button", name="Confirmer la remise", exact=True).click()
            expect(phone.locator("#request-status")).to_have_text("Livrée")
            pc.reload()
            expect(pc.locator("#request-status")).to_have_text("Livrée")
            expect(pc.get_by_role("button", name="Annuler ma demande", exact=True)).to_have_count(0)
            pc.screenshot(path=str(folder / "demandeur — profil A-livree.png"), full_page=True)
            phone.screenshot(path=str(folder / "livree-mobile.png"), full_page=True)
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]), [(8, 0, 8)] * 3
            )
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 0
            )
            self.assertEqual(errors, [])
            self.assertEqual(failures, [])
            browser.close()
        (folder / "parcours.json").write_text(
            json.dumps(
                {
                    "request_id": identifier,
                    "shared_database": True,
                    "after_send": {"P": 10, "R": 2, "D": 8},
                    "after_delivery": {"P": 8, "R": 0, "D": 8},
                    "products": 3,
                    "console_errors": errors,
                    "network_5xx": failures,
                    "physical_iphone": "NON_EXECUTE_M5",
                },
                indent=2,
            )
            + "\n"
        )

    def test_mobile_partial_then_shortage_and_error_report_without_edit(self):
        request, products = self.prepare()
        folder = self.evidence()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.phone(browser)
            self.login(phone, self.replenisher)
            phone.goto(self.live_server_url + f"/demandes/{request.pk}")
            phone.locator(".handed-quantity").fill("1")
            phone.get_by_label("Le reste sera apporté plus tard", exact=True).check()
            self.review(phone)
            phone.get_by_role("button", name="Confirmer la remise", exact=True).click()
            expect(phone.locator("#request-status")).to_have_text(
                "Partiellement livrée — reste à livrer"
            )
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (9, 3, 6))
            phone.locator(".handed-quantity").fill("1")
            phone.get_by_label("Le reste est réellement introuvable en réserve", exact=True).check()
            self.review(phone)
            expect(phone.locator("#delivery-summary")).to_contain_text("2 manquants clôturés")
            phone.screenshot(path=str(folder / "recap-manque-mobile.png"), full_page=True)
            phone.locator("#shortage-ack").check()
            phone.get_by_role("button", name="Confirmer la remise", exact=True).click()
            expect(phone.locator("#request-status")).to_have_text(
                "Partiellement livrée — clôturée pour manque"
            )
            phone.get_by_role("link", name="Signaler une erreur", exact=True).first.click()
            phone.get_by_label("Précision facultative", exact=True).fill(
                "Comptage de remise fictif à vérifier"
            )
            phone.get_by_role("button", name="Confirmer le signalement", exact=True).click()
            expect(
                phone.get_by_text("Une erreur a déjà été signalée pour cette remise.", exact=True)
            ).to_be_visible()
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 0, 6))
            self.assertEqual(self.db(lambda: DeliveryIssue.objects.count()), 2)
            browser.close()

    def test_committed_delivery_lost_response_double_click_reload_recovers_once(self):
        request, products = self.prepare()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.phone(browser)
            self.login(phone, self.replenisher)
            phone.goto(self.live_server_url + f"/demandes/{request.pk}")
            self.review(phone)

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 201)
                route.abort("failed")

            phone.route(f"**/api/v1/requests/{request.pk}/deliveries", lose, times=1)
            phone.locator("#delivery-confirm").evaluate(
                "(button)=>{button.click();button.click();}"
            )
            expect(phone.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            expect(phone.locator("#operation-recovery")).to_be_visible()
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            phone.reload()
            expect(phone.locator("#request-status")).to_have_text("Livrée")
            expect(phone.locator("#operation-recovery")).to_be_hidden()
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 0, 6))
            browser.close()

    def test_claim_lost_response_reload_resolves_before_allowing_delivery(self):
        request, _ = self.requested()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.phone(browser)
            self.login(phone, self.replenisher)
            phone.goto(self.live_server_url + f"/demandes/{request.pk}")

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort("failed")

            phone.route(f"**/api/v1/requests/{request.pk}/claim", lose, times=1)
            phone.get_by_role("button", name="Prendre en charge", exact=True).click()
            expect(phone.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            phone.reload()
            expect(phone.locator("#request-status")).to_have_text("En préparation")
            expect(phone.locator("#operation-recovery")).to_be_hidden()
            expect(
                phone.get_by_role("button", name="Vérifier la remise", exact=True)
            ).to_be_enabled()
            keys = phone.evaluate(
                'Object.keys(sessionStorage).filter(k=>k.includes("requests.claim"))'
            )
            self.assertEqual(keys, [])
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            browser.close()

    def test_expired_claim_after_other_tab_claim_switches_to_delivery_controller(self):
        request, _ = self.requested()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.phone(browser)
            self.login(phone, self.replenisher)
            phone.goto(self.live_server_url + f"/demandes/{request.pk}")

            def lose_registration(route):
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort("failed")

            phone.route("**/api/v1/operation-intents", lose_registration, times=1)
            # Attendre la vraie perte simulée, pas seulement le clic : route.fetch()
            # peut dépasser5s sur une suite complète chargée avant route.abort().
            with phone.expect_event(
                "requestfailed",
                predicate=lambda req: req.url.endswith("/api/v1/operation-intents"),
                timeout=15000,
            ):
                phone.get_by_role("button", name="Prendre en charge", exact=True).click()
            expect(phone.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            self.assertEqual(self.db(lambda: self.claim(request).status_code), 200)
            phone.reload()
            expect(phone.locator("#operation-result")).to_contain_text("Résultat encore incertain")
            phone.get_by_role("button", name="Résoudre cet envoi", exact=True).click()
            expect(
                phone.get_by_role("button", name="Vérifier la remise", exact=True)
            ).to_be_enabled()
            self.review(phone)
            phone.get_by_role("button", name="Confirmer la remise", exact=True).click()
            expect(phone.locator("#request-status")).to_have_text("Livrée")
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            browser.close()
