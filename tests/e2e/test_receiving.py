"""M8 : Chromium réel, téléphone émulé uniquement, aucun accès à l’iPhone physique."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.receiving.models import ArrivalReport, QuarantineItem, Receipt
from apps.receiving.state import item_state
from tests.e2e import test_deliveries as delivery_browser
from tests.test_receiving import ReceivingFixtures

ROOT = Path("docs/recette/M8/browser")


class ReceivingBrowserTests(ReceivingFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def setUp(self):
        super().setUp()
        ROOT.mkdir(parents=True, exist_ok=True)
        self.errors = []
        self.failures = []

    def page(self, browser, user, width=390, height=844):
        page = browser.new_page(
            viewport={"width": width, "height": height},
            is_mobile=width == 390,
            has_touch=width == 390,
            locale="fr-FR",
        )
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("response", lambda r: self.failures.append(r.url) if r.status >= 500 else None)
        self.login(page, user)
        return page

    def inspect(self, page, name):
        page.wait_for_load_state("networkidle")
        page.evaluate("window.scrollTo(0,0)")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        expect(page.locator('a[href="#"],a[href^="javascript:"]')).to_have_count(0)
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}));}"
        )
        self.assertEqual(violations, [], name)
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(
            json.dumps(
                {
                    "physical_iphone": False,
                    "viewport": page.viewport_size,
                    "axe_serious_critical": violations,
                    "js_errors": self.errors,
                    "http_5xx": self.failures,
                },
                indent=2,
            )
            + "\n"
        )

    def follow(self, page):
        page.get_by_role("link", name="Consulter le résultat", exact=True).click()
        page.wait_for_load_state("networkidle")

    def verify(self, page):
        page.get_by_label(
            "J’ai vérifié les faits et les conséquences de cette décision.", exact=True
        ).check()

    def test_mobile_partial_multi_product_then_complete_with_zero_new_values(self):
        products = [self.demandable_product(code=f"NAV-M8-{i}", physical=0) for i in range(2)]
        order = self.buy(products, [10, 5])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher)
            page.get_by_role("link", name="Réceptions", exact=True).click()
            page.get_by_role("link", name="Consulter la commande", exact=True).click()
            self.inspect(page, "commande-mobile")
            page.get_by_role("link", name="Recevoir cette arrivée", exact=True).click()
            values = page.locator('[data-quantity="conforming"]')
            self.assertEqual(values.evaluate_all("nodes=>nodes.map(n=>n.value)"), ["0", "0"])
            values.nth(0).fill("3")
            values.nth(1).fill("5")
            self.inspect(page, "reception-multi-mobile")
            page.get_by_role("button", name="Confirmer la réception", exact=True).click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("Reçu confirmé en lecture seule")
            self.assertEqual(self.db(lambda: [self.stock(p)[0] for p in products]), [3, 5])
            page.goto(self.live_server_url + f"/receptions/commandes/{order.pk}/recevoir")
            self.assertEqual(
                page.locator('[data-quantity="conforming"]').evaluate_all(
                    "nodes=>nodes.map(n=>n.value)"
                ),
                ["0", "0"],
            )
            page.locator('[data-quantity="conforming"]').nth(0).fill("7")
            page.get_by_role("button", name="Confirmer la réception", exact=True).click()
            self.follow(page)
            self.assertEqual(self.db(lambda: [self.stock(p)[0] for p in products]), [10, 5])
            expect(page.locator(".bottom-navigation a")).to_have_count(5)
            browser.close()
        self.assertEqual((self.errors, self.failures), ([], []))

    def test_admin_surplus_and_receipt_correction_preview_confirmation(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [20])
        self.receive(order, [23])
        receipt = Receipt.objects.get()
        item = QuarantineItem.objects.get()
        case = self.receipt_case(receipt)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin, 1440, 900)
            page.goto(self.live_server_url + f"/receptions/dossiers/{item.case_id}")
            self.inspect(page, "surplus-admin-1440")
            page.get_by_role("link", name="Accepter le surplus restant", exact=True).click()
            self.verify(page)
            page.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("Résolu")
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 23)
            page.goto(self.live_server_url + f"/receptions/dossiers/{case.pk}/correction")
            page.locator('[data-quantity="conforming"]').fill("24")
            page.get_by_label("Vérification effectuée", exact=True).fill(
                "Comptage vérifié sur le reçu fictif"
            )
            self.verify(page)
            page.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(page.locator("#arrival-preview")).to_be_visible()
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 23)
            self.inspect(page, "correction-admin-1440")
            page.get_by_role("button", name="Confirmer la correction", exact=True).click()
            self.follow(page)
            self.assertEqual(self.db(lambda: item_state(item.pk)["remaining"]), 1)
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 23)
            page.set_viewport_size({"width": 1280, "height": 800})
            self.inspect(page, "dossier-admin-1280")
            browser.close()
        self.assertEqual((self.errors, self.failures), ([], []))

    def test_unknown_colis_link_then_distinct_receipt(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [4])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.page(browser, self.replenisher)
            admin = self.page(browser, self.admin, 1440, 900)
            phone.goto(self.live_server_url + "/receptions/commande-introuvable")
            phone.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(phone)
            report_id, case_id = self.db(
                lambda: (
                    str(ArrivalReport.objects.get().pk),
                    str(ArrivalReport.objects.get().case_id),
                )
            )
            admin.goto(self.live_server_url + f"/receptions/dossiers/{case_id}")
            admin.get_by_role("link", name="Rattacher une commande", exact=True).click()
            admin.get_by_role("link", name="Choisir cette commande", exact=True).click()
            admin.locator('[data-quantity="quantity"]').fill("4")
            self.verify(admin)
            admin.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(admin)
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 0)
            phone.goto(self.live_server_url + f"/receptions/commandes/{order.pk}/recevoir")
            phone.locator("#arrival-source").select_option(report_id)
            phone.locator('[data-quantity="conforming"]').fill("4")
            phone.get_by_role("button", name="Confirmer la réception", exact=True).click()
            self.follow(phone)
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 4)
            browser.close()
        self.assertEqual((self.errors, self.failures), ([], []))

    def test_lost_response_check_real_result_no_second_credit_and_offline(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [4])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher)
            page.goto(self.live_server_url + f"/receptions/commandes/{order.pk}/recevoir")
            page.locator('[data-quantity="conforming"]').fill("4")
            page.context.set_offline(True)
            expect(page.locator("#arrival-submit")).to_be_disabled()
            expect(page.locator("#operation-network")).to_be_visible()
            page.context.set_offline(False)

            def lose(route):
                route.fetch()
                route.abort("failed")

            page.route(f"**/api/v1/orders/{order.pk}/receipts", lose)
            page.get_by_role("button", name="Confirmer la réception", exact=True).click()
            expect(page.locator("#operation-recovery")).to_be_visible()
            page.get_by_role("button", name="Vérifier l'enregistrement", exact=True).click()
            self.follow(page)
            self.assertEqual(self.db(lambda: Receipt.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 4)
            self.inspect(page, "resultat-apres-perte-reponse-mobile")
            browser.close()
        self.assertEqual((self.errors, self.failures), ([], []))

    def test_late_reinstate_distinct_receipt_and_surplus_refusal_verified(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [5])
        self.cancel_remainder(order, 5)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.page(browser, self.replenisher)
            admin = self.page(browser, self.admin, 1440, 900)
            phone.goto(self.live_server_url + f"/receptions/commandes/{order.pk}")
            phone.get_by_role("link", name="Signaler un reliquat annulé arrivé", exact=True).click()
            phone.locator('[data-quantity="quantity"]').fill("3")
            phone.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(phone)
            report_id, case_id = self.db(
                lambda: (
                    str(ArrivalReport.objects.get().pk),
                    str(ArrivalReport.objects.get().case_id),
                )
            )
            admin.goto(self.live_server_url + f"/receptions/dossiers/{case_id}")
            admin.get_by_role("link", name="Rétablir le reliquat annulé", exact=True).click()
            admin.locator('[data-quantity="quantity"]').fill("3")
            self.verify(admin)
            self.inspect(admin, "retablissement-admin")
            admin.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(admin)
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 0)
            phone.goto(self.live_server_url + f"/receptions/commandes/{order.pk}/recevoir")
            phone.locator("#arrival-source").select_option(report_id)
            phone.locator('[data-quantity="conforming"]').fill("3")
            phone.get_by_role("button", name="Confirmer la réception", exact=True).click()
            self.follow(phone)
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 3)
            browser.close()
        self.assertEqual((self.errors, self.failures), ([], []))

    def test_unknown_picker_real_search_and_refused_surplus_external_verification(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [2])
        self.receive(order, [5])
        item = QuarantineItem.objects.get()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            phone = self.page(browser, self.replenisher)
            admin = self.page(browser, self.admin, 1440, 900)
            phone.goto(self.live_server_url + "/receptions/commande-introuvable")
            phone.get_by_label("Produit identifiable — facultatif", exact=True).fill(p.code)
            phone.get_by_role("button", name="Ajouter au constat", exact=True).click()
            expect(
                phone.get_by_label("Quantité identifiable — facultative", exact=True)
            ).to_be_visible()
            phone.get_by_label("Quantité identifiable — facultative", exact=True).fill("4")
            phone.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(phone)
            self.assertEqual(self.db(lambda: ArrivalReport.objects.get().lines.get().quantity), 4)
            admin.goto(self.live_server_url + f"/receptions/dossiers/{item.case_id}")
            admin.get_by_role("link", name="Refuser le surplus", exact=True).click()
            self.verify(admin)
            admin.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(admin)
            expect(admin.locator("main")).to_contain_text("Surplus refusé")
            admin.get_by_role("link", name="Vérifier le traitement externe", exact=True).click()
            self.verify(admin)
            admin.get_by_role("button", name="Confirmer", exact=True).click()
            self.follow(admin)
            expect(admin.locator("main")).to_contain_text("Résolu")
            self.assertEqual(self.db(lambda: self.stock(p)[0]), 2)
            browser.close()
        self.assertEqual((self.errors, self.failures), ([], []))
