"""M4 : interface demandeur — profil A réelle, PostgreSQL et HTTP sans succès simulé."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from playwright.sync_api import expect, sync_playwright

from apps.inventory.models import StockEntry
from apps.requests.models import InternalRequest, RequestEvent, Reservation
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_requests import RequestFixtures


class BrowserRequestTests(RequestFixtures, StaticLiveServerTestCase):
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
        page.wait_for_load_state("networkidle")
        page.get_by_label("Identifiant", exact=True).fill(self.assistant.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")
        page.get_by_role("link", name="Faire une demande", exact=True).click()
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("heading", name="Faire une demande", exact=True)).to_be_visible()

    def evidence(self):
        path = Path("reports/m4-browser")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def send_selection(self, page):
        page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
        expect(
            page.get_by_role("heading", name="Récapitulatif avant envoi", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Envoyer la demande", exact=True).click()

    def test_assistant_selects_reviews_sends_follows_and_cancels_real_request(self):
        products = [self.demandable_product(code=f"NAV-{i}") for i in range(3)]
        evidence = self.evidence()
        errors = []
        failures = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="fr-FR")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    failures.append(response.status) if response.status >= 500 else None
                ),
            )
            self.login(page)
            expect(
                page.get_by_text("Unité fictif A · compte individuel", exact=True)
            ).to_be_visible()
            self.assertEqual(page.locator(".request-product").count(), 3)
            self.assertTrue(
                page.locator(".request-product img").first.evaluate(
                    "img=>img.complete && img.naturalWidth>0"
                )
            )
            for product, quantity in zip(products, [2, 3, 1], strict=True):
                page.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill(
                    str(quantity)
                )
            expect(page.locator("#cart-count")).to_have_text("3 références sélectionnées")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
            expect(page.locator("#request-review-lines li")).to_have_count(3)
            page.screenshot(path=str(evidence / "recapitulatif-pc.png"), full_page=True)
            context.set_offline(True)
            expect(
                page.get_by_role("button", name="Envoyer la demande", exact=True)
            ).to_be_disabled()
            expect(page.locator("#operation-network")).to_be_visible()
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            context.set_offline(False)
            page.get_by_role("button", name="Envoyer la demande", exact=True).click()
            expect(page.locator("#operation-result")).to_contain_text(
                "envoyée. Quantités réservées."
            )
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]),
                [(10, 2, 8), (10, 3, 7), (10, 1, 9)],
            )
            page.get_by_role("link", name="Suivre cette demande", exact=True).click()
            expect(page.locator("#request-status")).to_have_text("Nouvelle")
            page.wait_for_load_state("networkidle")
            expect(page.locator(".product-grid img")).to_have_count(3)
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.product-grid img')).every(img=>img.complete && img.naturalWidth>0)"
            )
            expect(page.get_by_text("Réservé pour cette demande", exact=True)).to_have_count(3)
            page.screenshot(path=str(evidence / "suivi-pc.png"), full_page=True)
            page.get_by_role("button", name="Annuler ma demande", exact=True).click()
            expect(
                page.get_by_role("heading", name="Confirmer l'annulation", exact=True)
            ).to_be_visible()
            page.get_by_role("button", name="Conserver ma demande", exact=True).click()
            self.assertEqual(
                self.db(lambda: Reservation.objects.filter(remaining__gt=0).count()), 3
            )
            page.get_by_role("button", name="Annuler ma demande", exact=True).click()
            page.get_by_role("button", name="Confirmer l'annulation", exact=True).click()
            expect(page.locator("#request-status")).to_have_text("Annulée")
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]), [(10, 0, 10)] * 3
            )
            page.get_by_role("link", name="Mon historique", exact=True).click()
            expect(page.get_by_role("heading", name="Mon historique", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="Demande annulée", exact=False)).to_be_visible()
            page.screenshot(path=str(evidence / "historique-pc.png"), full_page=True)
            self.assertEqual(errors, [])
            self.assertEqual(failures, [])
            browser.close()
        (evidence / "parcours.json").write_text(
            json.dumps(
                {
                    "browser": "Chromium headless",
                    "viewport": [1440, 1000],
                    "initial_physical": [10, 10, 10],
                    "reserved_after_send": [2, 3, 1],
                    "reserved_after_cancel": [0, 0, 0],
                    "console_errors": errors,
                    "network_5xx": failures,
                },
                indent=2,
            )
        )

    def test_lost_response_after_commit_and_double_click_recover_one_request(self):
        product = self.demandable_product()
        evidence = self.evidence()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1365, "height": 950})
            self.login(page)
            posts = []

            def lose(route):
                upstream = route.fetch()
                self.assertEqual(upstream.status, 201)
                posts.append(upstream.json()["operation_id"])
                route.abort("failed")

            page.route("**/api/v1/requests", lose, times=1)
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
            # Deux clics DOM rapprochés, avec vraie requête serveur : le second est neutralisé.
            page.get_by_role("button", name="Envoyer la demande", exact=True).evaluate(
                "button=>{button.click();button.click();}"
            )
            expect(page.locator("#operation-result")).to_contain_text("Vérifiez l'enregistrement")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            pending = page.evaluate(
                "JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(k=>k.startsWith('reserve.request.pending.'))))"
            )
            self.assertEqual(set(pending), {"intent_id", "key", "payload_hash", "command_type"})
            page.reload()
            expect(page.locator("#operation-result")).to_contain_text(
                "envoyée. Quantités réservées."
            )
            self.assertEqual(len(posts), 1)
            self.assertEqual(self.db(lambda: StockEntry.objects.filter(kind="RESERVE").count()), 1)
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (10, 1, 9))
            page.screenshot(path=str(evidence / "reprise-envoi.png"), full_page=True)
            browser.close()
        (evidence / "reprise.json").write_text(
            json.dumps(
                {
                    "real_post_executed": 1,
                    "real_reservation_entry": 1,
                    "lost_response_after_commit": True,
                    "automatic_replay": False,
                },
                indent=2,
            )
        )

    def test_changed_ceiling_refusal_keeps_selection_and_can_be_corrected(self):
        product = self.demandable_product()
        evidence = self.evidence()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1365, "height": 950})
            self.login(page)
            page.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("4")
            self.db(
                lambda: self.rights(
                    product,
                    [
                        {
                            "floor_id": str(self.floor.pk),
                            "visible": True,
                            "demandable": True,
                            "maximum": 2,
                        }
                    ],
                )
            )
            self.send_selection(page)
            expect(page.locator("#operation-result")).to_contain_text("Aucune demande envoyée")
            expect(
                page.get_by_label(f"Quantité demandée — {product.name}", exact=True)
            ).to_have_value("4")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            page.screenshot(path=str(evidence / "refus-plafond.png"), full_page=True)
            page.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("2")
            self.send_selection(page)
            expect(page.locator("#operation-result")).to_contain_text(
                "envoyée. Quantités réservées."
            )
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (10, 2, 8))
            browser.close()

    def test_lost_registration_and_lost_cancellation_are_resolved_safely(self):
        product = self.demandable_product()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1365, "height": 950})
            self.login(page)
            page.route("**/api/v1/operation-intents", lambda route: route.abort("failed"), times=1)
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            self.send_selection(page)
            expect(page.locator("#operation-result")).to_contain_text("Vérifiez l'enregistrement")
            page.reload()
            expect(page.locator("#operation-result")).to_contain_text(
                "Intention encore introuvable"
            )
            page.get_by_role("button", name="Résoudre cet envoi", exact=True).click()
            expect(page.locator("#operation-recovery")).to_be_hidden()
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            self.send_selection(page)
            expect(
                page.get_by_role("link", name="Suivre cette demande", exact=True)
            ).to_be_visible()
            page.get_by_role("link", name="Suivre cette demande", exact=True).click()

            def lose_cancel(route):
                upstream = route.fetch()
                self.assertEqual(upstream.status, 200)
                route.abort("failed")

            page.route("**/api/v1/requests/*/cancel", lose_cancel, times=1)
            page.get_by_role("button", name="Annuler ma demande", exact=True).click()
            page.get_by_role("button", name="Confirmer l'annulation", exact=True).click()
            expect(page.locator("#operation-result")).to_contain_text("Vérifiez l'enregistrement")
            page.reload()
            expect(page.locator("#request-status")).to_have_text("Annulée")
            self.assertEqual(self.db(lambda: StockEntry.objects.filter(kind="RELEASE").count()), 1)
            self.assertEqual(self.db(lambda: RequestEvent.objects.count()), 2)
            browser.close()

    def test_confirmed_result_survives_catalogue_refresh_failure(self):
        product = self.demandable_product()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1365, "height": 950})
            self.login(page)
            # L'envoi et sa lecture de statut atteignent réellement le serveur.
            # Seule la lecture suivante du catalogue est empêchée.
            page.route("**/api/v1/request-catalog*", lambda route: route.abort("failed"))
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            self.send_selection(page)
            expect(page.locator("#operation-result")).to_contain_text(
                "envoyée. Quantités réservées."
            )
            expect(page.locator("#operation-result")).to_contain_text(
                "Disponibilités non actualisées"
            )
            expect(
                page.get_by_role("link", name="Suivre cette demande", exact=True)
            ).to_be_visible()
            expect(
                page.get_by_label(f"Quantité demandée — {product.name}", exact=True)
            ).to_have_value("0")
            expect(page.locator("#cart-count")).to_have_text("Aucun produit sélectionné")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            page.get_by_role("link", name="Suivre cette demande", exact=True).click()
            expect(page.locator("#request-status")).to_have_text("Nouvelle")
            browser.close()
