"""M3 : navigateur Chromium réel, HTTP réel et PostgreSQL, données fictives."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from playwright.sync_api import expect, sync_playwright

from apps.cases.models import StockCase
from apps.catalog.models import Product
from apps.inventory.models import StockEntry
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_inventory import InventoryFixtures


class BrowserInventoryTests(InventoryFixtures, StaticLiveServerTestCase):
    def db(self, action):
        # Playwright exécute sa boucle asyncio ; les vraies requêtes ORM restent
        # dans un thread synchrone distinct, sans désactiver la garde Django.
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
        expect(page.get_by_label("Identifiant", exact=True)).to_be_visible()
        page.get_by_label("Identifiant", exact=True).fill(user.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")

    def observe(self, page, errors, server_errors):
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "response",
            lambda response: (
                server_errors.append(response.status) if response.status >= 500 else None
            ),
        )

    def test_mobile_initial_zero_admin_activation_and_three_product_single_confirmation(self):
        products = [
            self.create_product(code=f"DEMO-{i}", name=name)
            for i, name in enumerate(["Boîtes fictives", "Cartons fictifs", "Paquets fictifs"])
        ]
        for product in products:
            self.assertEqual(self.upload(product).status_code, 200)
        evidence = Path("reports/m3-browser")
        evidence.mkdir(parents=True, exist_ok=True)
        errors, server_errors = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            mobile = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                locale="fr-FR",
            )
            phone = mobile.new_page()
            self.observe(phone, errors, server_errors)
            self.login(phone, self.replenisher)
            phone.goto(self.live_server_url + "/stocks/inventaire-initial")
            phone.wait_for_load_state("networkidle")
            expect(
                phone.get_by_role("heading", name="Inventaire initial", exact=True)
            ).to_be_visible()
            self.assertEqual(phone.locator(".stock-row").count(), 3)
            for product, quantity in zip(products, [0, 10, 8], strict=True):
                phone.get_by_label(f"Quantité physique — {product.name}", exact=True).fill(
                    str(quantity)
                )
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), 0)
            expect(phone.locator(".stock-dirty")).to_have_count(3)
            phone.get_by_role("button", name="Confirmer l'inventaire", exact=True).click()
            expect(phone.locator("#stock-result")).to_contain_text("Lot enregistré une seule fois")
            self.assertEqual(self.db(lambda: [self.stock(p)[0] for p in products]), [0, 10, 8])
            expect(phone.locator(".stock-row")).to_have_count(0)
            desktop = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="fr-FR")
            pc = desktop.new_page()
            self.observe(pc, errors, server_errors)
            self.login(pc, self.admin)
            pc.goto(self.live_server_url + f"/catalogue/{products[0].pk}")
            pc.wait_for_load_state("networkidle")
            expect(pc.get_by_text("Sans fournisseur configuré.", exact=True)).to_be_visible()
            expect(pc.get_by_text("Physique 0", exact=False)).to_be_visible()
            # Les libellés proviennent des formulaires rendus et réellement soumis.
            for checkbox in pc.locator("input[type=checkbox]").all():
                checkbox.check()
            pc.get_by_role("button", name="Activer le produit", exact=True).click()
            expect(
                pc.get_by_text("Produit actif, vérifié par la gestionnaire.", exact=True)
            ).to_be_visible()
            self.assertEqual(
                self.db(lambda: Product.objects.get(pk=products[0].pk).status), "ACTIVE"
            )
            pc.goto(self.live_server_url + "/stocks")
            pc.wait_for_load_state("networkidle")
            expect(pc.get_by_text("Vue de supervision.", exact=False)).to_be_visible()
            expect(
                pc.get_by_role("button", name="Confirmer la mise à jour", exact=True)
            ).to_have_count(0)
            pc.screenshot(path=str(evidence / "supervision.png"), full_page=True)
            phone.goto(self.live_server_url + "/stocks")
            phone.wait_for_load_state("networkidle")
            expect(phone.locator(".stock-row img")).to_have_count(3)
            for product in products:
                phone.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            expect(phone.locator(".stock-dirty")).to_have_count(3)
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), 3)
            phone.get_by_role("button", name="Annuler", exact=True).click()
            expect(phone.locator(".stock-dirty")).to_have_count(0)
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), 3)
            for product, quantity in zip(products, [2, 7, 6], strict=True):
                phone.get_by_label(f"Quantité physique — {product.name}", exact=True).fill(
                    str(quantity)
                )
            mobile.set_offline(True)
            expect(
                phone.get_by_role("button", name="Confirmer la mise à jour", exact=True)
            ).to_be_disabled()
            expect(phone.locator("#stock-network")).to_be_visible()
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), 3)
            mobile.set_offline(False)
            expect(
                phone.get_by_role("button", name="Confirmer la mise à jour", exact=True)
            ).to_be_enabled()
            phone.screenshot(path=str(evidence / "lot-provisoire-mobile.png"), full_page=True)
            phone.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(phone.locator("#stock-result")).to_contain_text("Lot enregistré une seule fois")
            self.assertEqual(self.db(lambda: [self.stock(p)[0] for p in products]), [2, 7, 6])
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 3
            )
            phone.get_by_role("link", name="Mon activité", exact=True).click()
            expect(
                phone.get_by_role("heading", name="Mon activité de réserve", exact=True)
            ).to_be_visible()
            phone.screenshot(path=str(evidence / "activite-mobile.png"), full_page=True)
            self.assertLessEqual(phone.evaluate("document.documentElement.scrollWidth"), 390)
            self.assertEqual(errors, [])
            self.assertEqual(server_errors, [])
            browser.close()
        (evidence / "parcours.json").write_text(
            json.dumps(
                {
                    "browser": "Chromium headless",
                    "viewport_mobile": [390, 844],
                    "initial_quantities": [0, 10, 8],
                    "final_quantities": [2, 7, 6],
                    "ordinary_entries": 3,
                    "console_errors": errors,
                    "network_5xx": server_errors,
                    "supplier_required": False,
                },
                indent=2,
            )
        )

    def test_lost_http_response_recovered_then_stale_values_explicitly_rechecked(self):
        product = self.create_product(name="Paquets réseau fictifs")
        self.initial([product])
        evidence = Path("reports/m3-browser")
        evidence.mkdir(parents=True, exist_ok=True)
        errors, server_errors = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            page = context.new_page()
            self.observe(page, errors, server_errors)
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + "/stocks")
            page.wait_for_load_state("networkidle")
            expect(page.get_by_label(f"Quantité physique — {product.name}")).to_have_value("10")
            requests = []

            def lose_response(route):
                # Le serveur exécute et confirme réellement ; seule sa réponse est perdue.
                upstream = route.fetch()
                self.assertEqual(upstream.status, 200)
                requests.append(upstream.json()["operation_id"])
                route.abort("failed")

            page.route("**/api/v1/stocks/adjustments", lose_response, times=1)
            page.get_by_label(f"Quantité physique — {product.name}").fill("8")
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-result")).to_contain_text("Vérifiez l'enregistrement")
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 8)
            expect(page.locator("#stock-recovery")).to_be_visible()
            pending = page.evaluate(
                "JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(k=>k.startsWith('reserve.stock.pending.'))))"
            )
            self.assertEqual(set(pending), {"intent_id", "key", "command_type", "payload_hash"})
            page.reload()
            expect(page.locator("#stock-result")).to_contain_text("Lot enregistré une seule fois")
            expect(page.get_by_label(f"Quantité physique — {product.name}")).to_have_value("8")
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 1
            )
            page.get_by_label(f"Quantité physique — {product.name}").fill("7")
            self.assertEqual(self.db(lambda: self.adjust([product], [9]).status_code), 200)
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-conflicts")).to_be_visible()
            expect(page.locator("#conflict-values")).to_contain_text("votre saisie 7, serveur 9")
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 9)
            page.get_by_role(
                "button", name="Recontrôler avec les valeurs actuelles", exact=True
            ).click()
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 9)
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-result")).to_contain_text("Lot enregistré une seule fois")
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 7)
            page.screenshot(path=str(evidence / "reprise-reseau.png"), full_page=True)
            self.assertEqual(errors, [])
            self.assertEqual(server_errors, [])
            browser.close()
        (evidence / "reprise.json").write_text(
            json.dumps(
                {
                    "actual_server_operations_after_lost_response": 1,
                    "automatic_resubmission": False,
                    "final_quantity": 7,
                    "explicit_stale_review": True,
                    "console_errors": errors,
                },
                indent=2,
            )
        )

    def test_below_reservations_mobile_refusal_and_admin_case(self):
        product = self.reserved_product()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            page = context.new_page()
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + "/stocks")
            page.wait_for_load_state("networkidle")
            expect(page.get_by_text("Réservé 4 · Disponible 6 carton", exact=True)).to_be_visible()
            page.get_by_label(f"Quantité physique — {product.name}").fill("3")
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-result")).to_contain_text("Signalement transmis")
            self.assertEqual(self.db(lambda: self.stock(product)), (10, 4, 6, 2))
            case = self.db(lambda: StockCase.objects.get())
            page.get_by_role("link", name="Ouvrir le signalement", exact=True).click()
            expect(
                page.get_by_role("heading", name="Ce qui a été signalé", exact=True)
            ).to_be_visible()
            expect(page.locator("main")).not_to_contain_text(case.reference)
            desktop = browser.new_context()
            pc = desktop.new_page()
            self.login(pc, self.admin)
            pc.goto(self.live_server_url + f"/stocks/signalements/{case.pk}")
            pc.wait_for_load_state("networkidle")
            expect(
                pc.get_by_role("heading", name="Ce qui a été signalé", exact=True)
            ).to_be_visible()
            expect(
                pc.get_by_text("aucun mouvement de stock de cet ajustement", exact=False)
            ).to_be_visible()
            browser.close()

    def test_lost_registration_resolved_same_binding_after_reload(self):
        product = self.create_product(name="Boîtes avant envoi fictives")
        self.initial([product])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
            page = context.new_page()
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + "/stocks")
            page.wait_for_load_state("networkidle")
            page.route("**/api/v1/operation-intents", lambda route: route.abort("failed"), times=1)
            page.get_by_label(f"Quantité physique — {product.name}").fill("8")
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-result")).to_contain_text("Vérifiez l'enregistrement")
            page.reload()
            expect(page.locator("#stock-result")).to_contain_text("Intention encore introuvable")
            page.get_by_role("button", name="Résoudre cette confirmation", exact=True).click()
            expect(page.locator("#stock-recovery")).to_be_hidden()
            expect(page.locator("#stock-result")).to_contain_text("n'est plus utilisable")
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 10)
            page.get_by_label(f"Quantité physique — {product.name}").fill("8")
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-result")).to_contain_text("Lot enregistré une seule fois")
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 1
            )
            browser.close()
